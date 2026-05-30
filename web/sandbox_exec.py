# -*- coding: utf-8 -*-
"""Language-agnostic sandboxed compile+run machinery shared by the code runners.

Extracted from ``web.java_runner`` so the Java runner (``web/java_runner.py``) and
the C runner (``web/c_runner.py``) — and, transitively, the lesson validator
(``scripts/check_code_lessons.py``) — share ONE implementation of process spawning,
resource limits, environment scrubbing, output capping, the wall-clock timeout, and
the ``{pass,error,...}`` check finalization. One source of truth so the two
languages (and the validator) can't drift.

Deliberately **stdlib-only and FastAPI-free** so the CLI validator can import these
helpers without dragging in the web stack (``web/__init__.py`` is empty, so
``from web.sandbox_exec import ...`` is cheap and cycle-free).

Security: every phase spawns its compiler/runtime with a **scrubbed environment**
(no ``os.environ`` — that holds the API keys and ``APP_SECRET_KEY``), in a throwaway
temp dir, as its own process group with rlimits and a wall-clock timeout. This is
adequate ONLY inside the locked-down sandbox container (no internet, read-only FS,
dropped caps, pid/mem limits — see ``docker-compose.yml``); the in-process dev
fallback is NOT sandboxed and is for local testing only.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

try:  # POSIX-only; present on Linux + macOS, absent on Windows.
    import resource
except ImportError:  # pragma: no cover
    resource = None  # type: ignore[assignment]


def _int_env(name: str, default: int) -> int:
    """A positive int from the environment, else *default* (also for non-positive)."""
    try:
        value = int((os.environ.get(name) or "").strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


_MAX_OUTPUT = 64 * 1024          # chars of stdout/stderr surfaced to the caller
_READ_CAP = 256 * 1024           # bytes STORED per stream while draining (memory bound)
# Per-phase wall-clock seconds (compile, then run) and the bound on concurrent
# compile+run pipelines on the single sandbox process — both env-tunable so an
# operator can trade throughput for load without a code change. Defaults match the
# historical Java-runner values exactly, so today's behaviour is unchanged.
_PHASE_TIMEOUT = _int_env("CODE_SANDBOX_TIMEOUT", 10)
_SEM = asyncio.Semaphore(_int_env("CODE_SANDBOX_CONCURRENCY", 2))


def _child_limits() -> None:
    """preexec_fn: async-signal-safe only — new process group + rlimits.

    ``os.setsid()`` makes the child its own session/group leader so the parent can
    ``killpg`` the whole tree on timeout. We intentionally do NOT set
    ``RLIMIT_NPROC`` (per-uid → would throttle uvicorn; a fork bomb is bounded by the
    container ``pids_limit``) nor a tight ``RLIMIT_AS`` (the JVM reserves GBs of
    virtual memory; bound real memory via the container ``mem_limit`` instead). The
    CPU rlimit is a fixed backstop independent of the wall-clock timeout above.
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


def scrubbed_env(cwd: str, *, extra_path: str = "", extra_env: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal child environment — **never** ``os.environ`` (which holds the API
    keys + ``APP_SECRET_KEY``). ``extra_path`` is prepended to PATH (e.g. a JDK bin
    dir); ``extra_env`` adds non-secret vars (e.g. ``JAVA_HOME``). Falsy extra values
    are dropped."""
    env = {
        "PATH": f"{extra_path}:/usr/bin:/bin" if extra_path else "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "TMPDIR": cwd,
    }
    if extra_env:
        env.update({k: v for k, v in extra_env.items() if v})
    return env


def _cap(buf: bytes) -> str:
    if not buf:
        return ""
    return buf.decode("utf-8", errors="replace")[:_MAX_OUTPUT]


async def _read_capped(stream: asyncio.StreamReader, cap: int) -> bytes:
    """Read until EOF, STORING at most *cap* bytes but continuing to drain the
    rest so the writer never blocks on a full pipe. This bounds the server's
    memory under an output flood (``while(1) printf(...)``) while preserving normal
    completion for any program that exits on its own — even one with large-but-finite
    output (it's just truncated for display). Without this, ``communicate()`` buffered
    the entire flood into memory until the wall-clock timeout, which could OOM the
    shared sandbox process."""
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


async def _exec(cmd: list[str], cwd: str, stdin_bytes: bytes | None, timeout: int,
                *, env: dict[str, str]):
    """Run one phase. Returns (returncode|None, stdout, stderr, timed_out).

    stdout/stderr are read concurrently (avoids the classic full-pipe deadlock)
    and capped at ``_READ_CAP`` bytes each so a runaway program can't grow server
    memory without bound; the wall-clock *timeout* still kills the process group.
    The caller supplies the (scrubbed) ``env`` so this stays language-neutral.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
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


def compare_stdout(got: str, check: Any) -> bool:
    """stdout match with the worker's strip/normalize rule."""
    check = check or {}
    a = got
    b = str(check.get("expected") if check.get("expected") is not None else "")
    if check.get("normalize") in (None, "strip"):
        a = a.strip()
        b = b.strip()
    return a == b


def _finalize(result: dict[str, Any], check: Any) -> dict[str, Any]:
    """Add {pass,error,output,expected,kind} for a check, matching the worker.

    Generic over the two compiled-language check kinds (``stdout`` / ``harness``);
    both Java and C use identical semantics, so this lives here, not per-runner.
    """
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
