# -*- coding: utf-8 -*-
"""Server-side Java compile+run, shared with the lesson validator.

This module is the single source of truth for how a *Java* task is laid out and
checked, so the server endpoint (``web.routes.code_run``), the browser worker
(``web/static/js/code-worker-java.js``), and the lesson validator
(``scripts/check_code_lessons.py``) all agree — *validator-pass ⇒ server-pass*.

The language-agnostic sandbox machinery (process spawning, rlimits, env scrubbing,
output capping, the wall-clock timeout, ``compare_stdout``, and the check
``_finalize``) lives in ``web.sandbox_exec`` and is shared with the C runner
(``web/c_runner.py``). This module keeps only the Java specifics: the toolchain
constants, the file/class-name layout (``build_files``/``derive_class_name``), and
the ``run_java`` orchestration. ``compare_stdout`` is re-exported below so the
validator's existing ``from web.java_runner import compare_stdout`` keeps working.

Deliberately **stdlib-only and FastAPI-free** so the CLI validator can import the
pure helpers without dragging in the web stack.

Security: see ``web.sandbox_exec`` — the runner spawns ``javac``/``java`` with a
scrubbed environment, in a throwaway temp dir, as its own process group with rlimits
and a wall-clock timeout. This is adequate ONLY inside the locked-down sandbox
container; the in-process fallback in ``code_run`` is NOT sandboxed (dev only).
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from .sandbox_exec import (  # shared, language-agnostic core
    _PHASE_TIMEOUT,
    _SEM,
    _cap,
    _exec,
    _finalize,
    compare_stdout,  # re-exported for scripts/check_code_lessons.py — do not remove
    scrubbed_env,
)

__all__ = ["JAVA", "JAVAC", "JAVA_RELEASE", "TYPE_DECL_RE", "build_files",
           "compare_stdout", "derive_class_name", "run_java"]

# Language level pinned to 8 to match the CheerpJ runtime (version 8) the browser
# uses, so a feature that compiles here but not in-browser is caught the same way.
JAVA_RELEASE = "8"
JAVAC = shutil.which("javac")
JAVA = shutil.which("java")

PUBLIC_TYPE_RE = re.compile(r"\bpublic\s+(?:final\s+|abstract\s+)?(?:class|interface|enum)\s+([A-Za-z_]\w*)")
TYPE_DECL_RE = re.compile(r"\b(?:class|interface|enum)\s+([A-Za-z_]\w*)")
_CLASS_RE = re.compile(r"^[A-Za-z_]\w*$")
_FILE_RE = re.compile(r"^[A-Za-z_]\w*\.java$")

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


# ---- async runner (server endpoint) ------------------------------------------

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

            env = scrubbed_env(tmp, extra_path=_JAVA_BIN, extra_env={"JAVA_HOME": _JAVA_HOME})

            rc, _out, cerr, timed = await _exec(
                [JAVAC, "--release", JAVA_RELEASE, "-proc:none", *srcs.keys()],
                tmp, None, timeout, env=env,
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
                tmp, (stdin or "").encode("utf-8"), timeout, env=env,
            )
            result.update(
                stdout=_cap(out2), stderr=_cap(err2),
                exit_code=rc2, timed_out=timed2,
                ms=int((time.monotonic() - t0) * 1000),
            )
            return _finalize(result, check)
