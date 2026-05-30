# -*- coding: utf-8 -*-
"""Server-side Java compile+run, shared with the lesson validator.

This module is the single source of truth for how a Java task is laid out and
checked, so the server endpoint (``web.routes.code_run``), the browser worker
(``web/static/js/code-worker-java.js``), and the lesson validator
(``scripts/check_code_lessons.py``) all agree — *validator-pass ⇒ server-pass*.

Deliberately **stdlib-only and FastAPI-free** so the CLI validator can import the
pure helpers without dragging in the web stack (``web/__init__.py`` is empty, so
``from web.java_runner import ...`` is cheap and cycle-free).

Security: the runner spawns ``javac``/``java`` with a **scrubbed environment** (no
``os.environ`` — that holds the API keys and ``APP_SECRET_KEY``), in a throwaway
temp dir, as its own process group with rlimits and a wall-clock timeout. This is
adequate ONLY behind the dev token in ``code_run``; it does NOT stop a hostile
program from reading ``/proc/1/environ`` or the bind-mounts (same uid + PID ns as
uvicorn). Real isolation (a locked-down sandbox container) is required before this
is opened beyond the developer. See the plan / ``code_run`` docstring.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

try:  # POSIX-only; present on Linux + macOS, absent on Windows.
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]

# Language level pinned to 8 to match the CheerpJ runtime (version 8) the browser
# uses, so a feature that compiles here but not in-browser is caught the same way.
JAVA_RELEASE = "8"
JAVAC = shutil.which("javac")
JAVA = shutil.which("java")

PUBLIC_TYPE_RE = re.compile(r"\bpublic\s+(?:final\s+|abstract\s+)?(?:class|interface|enum)\s+([A-Za-z_]\w*)")
TYPE_DECL_RE = re.compile(r"\b(?:class|interface|enum)\s+([A-Za-z_]\w*)")
_CLASS_RE = re.compile(r"^[A-Za-z_]\w*$")
_FILE_RE = re.compile(r"^[A-Za-z_]\w*\.java$")

_MAX_OUTPUT = 64 * 1024          # chars of stdout/stderr surfaced to the caller
_READ_CAP = 256 * 1024           # bytes STORED per stream while draining (memory bound)
_PHASE_TIMEOUT = 10              # per-phase wall-clock seconds (compile, then run)
_SEM = asyncio.Semaphore(2)      # bound concurrent compiles on the single uvicorn proc

# JDK home derived from the resolved binary (non-secret); helps a wrapper `java`
# locate its runtime under the scrubbed env. java itself usually self-locates.
_JAVA_HOME = os.path.dirname(os.path.dirname(os.path.realpath(JAVA))) if JAVA else ""
_JAVA_BIN = os.path.dirname(JAVA) if JAVA else ""


# ---- pure helpers (shared with the validator) --------------------------------

def derive_class_name(src: str) -> str:
    """Public top-level type name (Java requires it to match the filename)."""
    m = PUBLIC_TYPE_RE.search(src or "") or TYPE_DECL_RE.search(src or "")
    return m.group(1) if m else "Main"


def build_files(code: str, files: Any, check: Any) -> tuple[dict[str, str], str]:
    """{filename: source} + main class — the worker's exact layout.

    ``code`` is the student/solution source; ``files`` optional support sources;
    ``check`` the task check spec. For ``kind: harness`` the harness source
    (``check.code``) is compiled alongside and is the entry class.
    """
    check = check or {}
    out: dict[str, str] = {}
    if isinstance(files, dict):
        out.update({str(k): str(v) for k, v in files.items()})
    cls = derive_class_name(code)
    out[f"{cls}.java"] = code
    main_class = check.get("main_class") or cls
    if check.get("kind") == "harness":
        hc = check.get("main_class") or "Harness"
        out[f"{hc}.java"] = check.get("code") or ""
        main_class = hc
    return out, main_class


def compare_stdout(got: str, check: Any) -> bool:
    """stdout match with the worker's strip/normalize rule."""
    check = check or {}
    a = got
    b = str(check.get("expected") if check.get("expected") is not None else "")
    if check.get("normalize") in (None, "strip"):
        a = a.strip()
        b = b.strip()
    return a == b


# ---- async runner (server endpoint) ------------------------------------------

def _child_limits() -> None:
    """preexec_fn: async-signal-safe only — new process group + rlimits.

    ``os.setsid()`` makes the child its own session/group leader so the parent can
    ``killpg`` the whole tree on timeout. We intentionally do NOT set
    ``RLIMIT_NPROC`` (per-uid → would throttle uvicorn) nor a tight ``RLIMIT_AS``
    (the JVM reserves GBs of virtual memory; bound real memory via the container
    ``mem_limit`` instead).
    """
    os.setsid()
    if resource is None:
        return

    def _set(res: int, soft: int, hard: int) -> None:
        try:
            resource.setrlimit(res, (soft, hard))
        except (ValueError, OSError):  # unsupported on this OS (e.g. some on macOS)
            pass

    _set(resource.RLIMIT_CPU, 10, 12)
    _set(resource.RLIMIT_FSIZE, 16 * 1024 * 1024, 16 * 1024 * 1024)
    _set(resource.RLIMIT_CORE, 0, 0)


def _scrubbed_env(cwd: str) -> dict[str, str]:
    env = {
        "PATH": f"{_JAVA_BIN}:/usr/bin:/bin" if _JAVA_BIN else "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": cwd,
    }
    if _JAVA_HOME:
        env["JAVA_HOME"] = _JAVA_HOME
    return env


def _cap(buf: bytes) -> str:
    if not buf:
        return ""
    return buf.decode("utf-8", errors="replace")[:_MAX_OUTPUT]


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    """Read until EOF, STORING at most *cap* bytes but continuing to drain the
    rest so the writer never blocks on a full pipe. This bounds the server's
    memory under an output flood (``while(true) System.out.println(...)``) while
    preserving normal completion for any program that exits on its own — even one
    with large-but-finite output (it's just truncated for display). Without this,
    ``communicate()`` buffered the entire flood into memory until the wall-clock
    timeout, which could OOM the shared sandbox process."""
    buf = bytearray()
    while True:
        chunk = await stream.read(65536)
        if not chunk:
            break
        if len(buf) < cap:
            buf += chunk[: cap - len(buf)]
        # beyond cap: discard but keep reading so the child can't block on write
    return bytes(buf)


async def _feed_stdin(stdin: asyncio.StreamWriter | None, data: bytes | None) -> None:
    if stdin is None or data is None:
        return
    try:
        stdin.write(data)
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass  # child closed stdin early / exited — fine
    finally:
        try:
            stdin.close()
        except Exception:
            pass


async def _exec(cmd: list[str], cwd: str, stdin_bytes: bytes | None, timeout: int):
    """Run one phase. Returns (returncode|None, stdout, stderr, timed_out).

    stdout/stderr are read concurrently (avoids the classic full-pipe deadlock)
    and capped at ``_READ_CAP`` bytes each so a runaway program can't grow server
    memory without bound; the wall-clock *timeout* still kills the process group.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_scrubbed_env(cwd),
        preexec_fn=_child_limits,
    )

    async def _collect() -> tuple[int | None, bytes, bytes]:
        out, err, _ = await asyncio.gather(
            _read_capped(proc.stdout, _READ_CAP),
            _read_capped(proc.stderr, _READ_CAP),
            _feed_stdin(proc.stdin, stdin_bytes),
        )
        rc = await proc.wait()
        return rc, out, err

    try:
        rc, out, err = await asyncio.wait_for(_collect(), timeout)
        return rc, out, err, False
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)   # group leader via setsid()
        except (ProcessLookupError, PermissionError):
            pass
        try:
            await proc.wait()                     # reap before the tempdir is torn down
        except Exception:
            pass
        return None, b"", b"", True


def _finalize(result: dict[str, Any], check: Any) -> dict[str, Any]:
    """Add {pass,error,output,expected,kind} for a check, matching the worker."""
    check = check or {}
    kind = check.get("kind")
    if not kind:
        return result  # free run — caller uses stdout/stderr/compile_errors
    result["kind"] = kind
    result["expected"] = str(check.get("expected")) if check.get("expected") is not None else None
    result["output"] = result["stdout"]
    if result.get("compile_errors"):
        result["pass"], result["error"] = False, result["compile_errors"]
    elif result.get("timed_out"):
        result["pass"], result["error"] = False, "Timed out."
    elif kind == "harness":
        ok = result["exit_code"] == 0
        result["pass"] = ok
        result["error"] = None if ok else (result["stderr"] or f"Program exited with code {result['exit_code']}")
    else:  # stdout
        if result["exit_code"] != 0:
            result["pass"], result["error"] = False, (result["stderr"] or f"Program exited with code {result['exit_code']}")
        else:
            result["pass"], result["error"] = compare_stdout(result["stdout"], check), None
    return result


async def run_java(
    code: str,
    files: Any = None,
    stdin: str = "",
    check: Any = None,
    *,
    timeout: int = _PHASE_TIMEOUT,
) -> dict[str, Any]:
    """Compile then run student Java in a throwaway temp dir; return a result dict
    ``{stdout, stderr, compile_errors, exit_code, timed_out, ms}`` plus
    ``{pass, error, output, expected, kind}`` when ``check`` is provided."""
    check = check or {}
    t0 = time.monotonic()
    result: dict[str, Any] = {
        "stdout": "", "stderr": "", "compile_errors": None,
        "exit_code": None, "timed_out": False, "ms": 0,
    }

    if not JAVAC or not JAVA:
        result["compile_errors"] = "Java toolchain is not available on the server."
        return _finalize(result, check)

    try:
        srcs, main_class = build_files(code, files, check)
    except Exception as e:  # pragma: no cover - defensive
        result["compile_errors"] = f"Could not assemble sources: {e}"
        return _finalize(result, check)

    if not _CLASS_RE.match(main_class):
        result["compile_errors"] = f"Invalid main class name: {main_class!r}"
        return _finalize(result, check)
    for name in srcs:
        if not _FILE_RE.match(name):
            result["compile_errors"] = f"Invalid file name: {name!r}"
            return _finalize(result, check)

    async with _SEM:
        with tempfile.TemporaryDirectory(prefix="javarun_") as tmp:
            for name, src in srcs.items():
                (Path(tmp) / name).write_text(src, encoding="utf-8")

            rc, _out, cerr, timed = await _exec(
                [JAVAC, "--release", JAVA_RELEASE, "-proc:none", *srcs.keys()],
                tmp, None, timeout,
            )
            if timed:
                result.update(timed_out=True, compile_errors="Compilation timed out.",
                              ms=int((time.monotonic() - t0) * 1000))
                return _finalize(result, check)
            if rc != 0:
                result.update(compile_errors=_cap(cerr), exit_code=1,
                              ms=int((time.monotonic() - t0) * 1000))
                return _finalize(result, check)

            rc2, out2, err2, timed2 = await _exec(
                [JAVA, "-cp", ".", main_class],
                tmp, (stdin or "").encode("utf-8"), timeout,
            )
            result.update(
                stdout=_cap(out2), stderr=_cap(err2),
                exit_code=rc2, timed_out=timed2,
                ms=int((time.monotonic() - t0) * 1000),
            )
            return _finalize(result, check)
