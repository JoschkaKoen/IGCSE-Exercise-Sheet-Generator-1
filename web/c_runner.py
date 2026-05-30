# -*- coding: utf-8 -*-
"""Server-side C compile+run, shared with the lesson validator.

The C analog of ``web.java_runner``: single source of truth for how a *C* task is
laid out and checked, so the server endpoint (``web.routes.code_run``) and the
lesson validator (``scripts/check_code_lessons.py``) agree — *validator-pass ⇒
server-pass*. The language-agnostic sandbox machinery (process spawning, rlimits,
env scrubbing, output capping, wall-clock timeout, ``_finalize``) is shared via
``web.sandbox_exec``; this module adds only the gcc specifics.

**The C harness pattern (vs Java).** Java lets a no-``main`` student class coexist
with a ``main``-bearing harness class. C cannot link two ``main``s, so for a
``kind: harness`` task the student writes *functions only* (saved to ``student.c``)
and the harness (``check.code``) is a complete translation unit that does
``#include "student.c"`` then defines ``main()``. We compile **only** ``harness.c``
— one translation unit, one ``main``, full visibility of the student's definitions,
no separate header to keep in sync. The student never sees ``harness.c``; its path
is scrubbed from any error output.

Deliberately **stdlib-only and FastAPI-free** so the CLI validator can import the
pure helpers (``CC``, ``CC_FLAGS``, ``build_c_files``) without the web stack.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .sandbox_exec import _PHASE_TIMEOUT, _SEM, _cap, _exec, _finalize, scrubbed_env

__all__ = ["CC", "CC_FLAGS", "build_c_files", "run_c"]

CC = shutil.which("gcc")
# Shared by the server and the validator (imported there) so the compile invocation
# can't drift. No -Werror: warnings inform but never fail a pedagogically-valid
# program. -g0 keeps the binary tiny (under RLIMIT_FSIZE); -O0 keeps behaviour
# predictable. NOTE: callers MUST append "-lm" AFTER the sources (see run_c).
CC_FLAGS = ["-std=c11", "-O0", "-g0", "-Wall", "-Wextra"]

# Valid C source/header filename: no path separators, must end .c or .h.
_C_FILE_RE = re.compile(r"^[A-Za-z_][\w.]*\.(c|h)$")

_MAIN_C = "main.c"        # stdout / free-run: the student writes a full program here
_STUDENT_C = "student.c"  # harness: the student's functions (no main) live here
_HARNESS_C = "harness.c"  # harness: the checker TU that #includes student.c + has main


def build_c_files(code: str, files: Any, check: Any) -> tuple[dict[str, str], list[str]]:
    """``({filename: source}, compile_sources)``. ``compile_sources`` is the list of
    files passed to gcc — the C analog of ``build_files``'s ``main_class`` (C always
    runs ``./main``, so there is no class name to return).

    For ``kind: harness`` we write both ``student.c`` and ``harness.c`` but compile
    **only ``harness.c``** — it pulls in ``student.c`` via ``#include`` (one TU, one
    ``main``). For every other kind the student's source is a full program in
    ``main.c``. Optional support ``.c`` files (``files``) are compiled alongside.
    """
    check = check or {}
    out: dict[str, str] = {}
    if isinstance(files, dict):
        out.update({str(k): str(v) for k, v in files.items()})
    extra_c = [n for n in out if n.endswith(".c")]
    if check.get("kind") == "harness":
        out[_STUDENT_C] = code
        out[_HARNESS_C] = check.get("code") or ""
        # student.c is included textually by harness.c → NOT compiled directly.
        sources = [_HARNESS_C] + [n for n in extra_c if n not in (_STUDENT_C, _HARNESS_C)]
    else:
        out[_MAIN_C] = code
        sources = [_MAIN_C] + [n for n in extra_c if n != _MAIN_C]
    return out, sources


def _scrub_harness_paths(text: str) -> str:
    """Keep the hidden harness invisible: drop ``#include`` context noise and
    relabel the internal filenames in any compiler/runtime message a student sees
    (``harness.c`` → ``checker``, ``student.c`` → ``your code``)."""
    if not text:
        return text
    out = []
    for line in text.splitlines():
        if "In file included from" in line:
            continue  # "In file included from harness.c:2:" — pure noise
        out.append(line.replace(_HARNESS_C, "checker").replace(_STUDENT_C, "your code"))
    return "\n".join(out)


async def run_c(
    code: str,
    files: Any = None,
    stdin: str = "",
    check: Any = None,
    *,
    timeout: int = _PHASE_TIMEOUT,
) -> dict[str, Any]:
    """Compile then run student C in a throwaway temp dir; return a result dict
    ``{stdout, stderr, compile_errors, exit_code, timed_out, ms}`` plus
    ``{pass, error, output, expected, kind}`` when ``check`` is provided — the exact
    shape ``run_java`` returns, so the client renderer is shared."""
    check = check or {}
    is_harness = check.get("kind") == "harness"
    t0 = time.monotonic()
    result: dict[str, Any] = {
        "stdout": "", "stderr": "", "compile_errors": None,
        "exit_code": None, "timed_out": False, "ms": 0,
    }

    def _ms() -> int:
        return int((time.monotonic() - t0) * 1000)

    if not CC:
        result["compile_errors"] = "C toolchain is not available on the server."
        return _finalize(result, check)

    try:
        srcs, sources = build_c_files(code, files, check)
    except Exception as e:  # pragma: no cover - defensive
        result["compile_errors"] = f"Could not assemble sources: {e}"
        return _finalize(result, check)

    for name in srcs:
        if not _C_FILE_RE.match(name):
            result["compile_errors"] = f"Invalid file name: {name!r}"
            return _finalize(result, check)

    async with _SEM:
        with tempfile.TemporaryDirectory(prefix="crun_") as tmp:
            for name, src in srcs.items():
                (Path(tmp) / name).write_text(src, encoding="utf-8")
            env = scrubbed_env(tmp)

            # Harness pre-check: syntax-check the student file ALONE so a student
            # error is attributed to student.c with their own line numbers, never
            # surfaced through the #include as "In file included from harness.c".
            if is_harness and _STUDENT_C in srcs:
                rc0, _o0, e0, timed0 = await _exec(
                    [CC, *CC_FLAGS, "-fsyntax-only", _STUDENT_C], tmp, None, timeout, env=env,
                )
                if timed0:
                    result.update(timed_out=True, compile_errors="Compilation timed out.", ms=_ms())
                    return _finalize(result, check)
                if rc0 != 0:
                    result.update(compile_errors=_scrub_harness_paths(_cap(e0)), exit_code=1, ms=_ms())
                    return _finalize(result, check)

            # Compile. -lm MUST be last so the linker resolves math.h symbols.
            rc, _out, cerr, timed = await _exec(
                [CC, *CC_FLAGS, *sources, "-o", "main", "-lm"], tmp, None, timeout, env=env,
            )
            if timed:
                result.update(timed_out=True, compile_errors="Compilation timed out.", ms=_ms())
                return _finalize(result, check)
            if rc != 0:
                cerr_txt = _cap(cerr)
                result.update(
                    compile_errors=_scrub_harness_paths(cerr_txt) if is_harness else cerr_txt,
                    exit_code=1, ms=_ms(),
                )
                return _finalize(result, check)

            # Run.
            rc2, out2, err2, timed2 = await _exec(
                ["./main"], tmp, (stdin or "").encode("utf-8"), timeout, env=env,
            )
            err_txt = _cap(err2)
            result.update(
                stdout=_cap(out2),
                stderr=_scrub_harness_paths(err_txt) if is_harness else err_txt,
                exit_code=rc2, timed_out=timed2, ms=_ms(),
            )
            return _finalize(result, check)
