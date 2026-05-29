#!/usr/bin/env python3
"""Validate authored Code-page lessons under ``content/code/``.

For every course (a directory with ``course.yaml``) and every lesson it lists, check:

  - **Schema** — ``meta.yaml`` has a bilingual ``title``; each task has ``id``, a
    bilingual ``prompt``, and a valid ``check`` (``stdout`` needs ``expected``;
    ``asserts`` needs ``code``).
  - **Parallel languages** — ``NN.en.md`` and ``NN.zh.md`` exist and split into the
    SAME number of ``---`` step chunks.
  - **Python validity** — every ```` ```python ```` example block, every ``starter``,
    every ``solution``, and every ``asserts`` ``code`` compiles.
  - **Solvability (the key gate)** — each task's reference ``solution`` passes its own
    ``check``, run in a subprocess (clean temp cwd, short timeout) that mirrors the
    browser worker (``web/static/js/code-worker.js``): ``stdout`` compares stripped
    (when ``normalize`` is ``strip``/unset); ``asserts`` execs the solution then the
    check code in one namespace; ``stdin`` is fed then EOF.
  - **No leakage** — ``solution`` never appears in ``load_lesson(...)['tasks_client_json']``.

A missing ``solution`` is a warning (the solvability gate can't run for that task).
Exit 0 if all good, 1 on any error.

Run:  .venv/bin/python scripts/check_code_lessons.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from web import code_content  # noqa: E402
from web.java_runner import (  # noqa: E402  — single source of truth, shared with the server runner
    JAVA,
    JAVA_RELEASE,
    JAVAC,
    TYPE_DECL_RE,
    build_files,
    compare_stdout,
    derive_class_name,
)

PY = sys.executable
SPLIT_RE = re.compile(r"(?m)^\s*-{3,}\s*$")
PY_FENCE_RE = re.compile(r"```python\n(.*?)\n```", re.S)
JAVA_FENCE_RE = re.compile(r"```java\n(.*?)\n```", re.S)
TIMEOUT = 10
# JAVAC / JAVA / JAVA_RELEASE / TYPE_DECL_RE and the file-layout + stdout-compare
# helpers are imported from web.java_runner above, so the validator and the server
# runner can never drift. A missing JDK makes the Java checks a skipped warning.

# Subprocess harness — mirrors the browser worker's check logic. argv[1] is a JSON
# task spec {solution, stdin, check}. Exits non-zero (with a message on stderr) if
# the reference solution does not pass its check.
HARNESS = r'''
import json, sys, io
spec = json.load(open(sys.argv[1]))
chk = spec.get("check") or {}
sys.stdin = io.StringIO(spec.get("stdin") or "")
buf = io.StringIO(); real = sys.stdout; sys.stdout = buf
ns = {}
try:
    exec(compile(spec["solution"], "<solution>", "exec"), ns)
    if chk.get("kind") == "asserts":
        exec(compile(chk.get("code") or "", "<check>", "exec"), ns)
finally:
    sys.stdout = real
out = buf.getvalue()
if chk.get("kind") == "stdout":
    norm = chk.get("normalize")
    a = out.strip() if norm in (None, "strip") else out
    b = str(chk.get("expected") if chk.get("expected") is not None else "")
    b = b.strip() if norm in (None, "strip") else b
    if a != b:
        sys.stderr.write("stdout mismatch:\n  got: %r\n  exp: %r\n" % (a, b))
        sys.exit(1)
'''

errors: list[str] = []
warnings: list[str] = []
_jdk_warned = False


def err(where: str, msg: str) -> None:
    errors.append(f"[FAIL] {where}: {msg}")


def warn(where: str, msg: str) -> None:
    warnings.append(f"[warn] {where}: {msg}")


def compile_ok(where: str, label: str, src: str) -> None:
    try:
        compile(src, label, "exec")
    except SyntaxError as e:
        err(where, f"{label} does not compile: {e}")


def run_solution(where: str, task: dict) -> None:
    spec = {
        "solution": task["solution"],
        "stdin": task.get("stdin") or "",
        "check": task.get("check") or {},
    }
    with tempfile.TemporaryDirectory() as tmp:
        specfile = Path(tmp) / "spec.json"
        specfile.write_text(json.dumps(spec), encoding="utf-8")
        try:
            r = subprocess.run(
                [PY, "-c", HARNESS, str(specfile)],
                cwd=tmp, capture_output=True, text=True, timeout=TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            err(where, f"solution timed out (>{TIMEOUT}s) — infinite loop?")
            return
    if r.returncode != 0:
        err(where, "reference solution does not pass its check:\n  " +
            (r.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))


# ---- Java path (compile/run helpers shared via web.java_runner) ---------------

def java_compile_ok(where: str, label: str, src: str, severity: str = "err") -> None:
    """javac --release 8 a full compilation unit. Snippets without a top-level
    type (illustrative fences) are skipped, as are runs with no JDK."""
    if not JAVAC or not TYPE_DECL_RE.search(src or ""):
        return
    report = err if severity == "err" else warn
    with tempfile.TemporaryDirectory() as tmp:
        name = f"{derive_class_name(src)}.java"
        (Path(tmp) / name).write_text(src, encoding="utf-8")
        try:
            r = subprocess.run([JAVAC, "--release", JAVA_RELEASE, name],
                               cwd=tmp, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            report(where, f"{label}: javac timed out")
            return
        if r.returncode != 0:
            report(where, f"{label} does not compile:\n  " +
                   (r.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))


def run_solution_java(where: str, task: dict) -> None:
    """Compile the reference solution (+ files + harness) and run it, applying the
    same check rules as the worker: stdout strip/normalize, harness = exit 0."""
    if not (JAVAC and JAVA):
        return
    chk = task.get("check") or {}
    kind = chk.get("kind")
    files, main_class = build_files(task["solution"], task.get("files"), chk)
    with tempfile.TemporaryDirectory() as tmp:
        for name, s in files.items():
            (Path(tmp) / name).write_text(s, encoding="utf-8")
        try:
            c = subprocess.run([JAVAC, "--release", JAVA_RELEASE, *files.keys()],
                               cwd=tmp, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            err(where, "solution javac timed out")
            return
        if c.returncode != 0:
            err(where, "reference solution does not compile:\n  " +
                (c.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))
            return
        try:
            r = subprocess.run([JAVA, "-cp", ".", main_class],
                               cwd=tmp, capture_output=True, text=True, timeout=TIMEOUT,
                               input=task.get("stdin") or "")
        except subprocess.TimeoutExpired:
            err(where, f"solution run timed out (>{TIMEOUT}s) — infinite loop?")
            return
        if kind == "harness":
            if r.returncode != 0:
                err(where, "reference solution fails its harness:\n  " +
                    (r.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))
            return
        # stdout
        if r.returncode != 0:
            err(where, "reference solution threw at runtime:\n  " +
                (r.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))
            return
        if not compare_stdout(r.stdout, chk):
            norm = chk.get("normalize")
            got = r.stdout.strip() if norm in (None, "strip") else r.stdout
            exp = str(chk.get("expected") if chk.get("expected") is not None else "")
            exp = exp.strip() if norm in (None, "strip") else exp
            err(where, "reference solution does not pass its check:\n  got: %r\n  exp: %r" % (got, exp))


def check_lesson(slug: str, nn: str, language: str = "python") -> None:
    cdir = code_content.course_dir(slug)
    where = f"{slug}/{nn}"
    meta = code_content._read_yaml(cdir / f"{nn}.meta.yaml")
    if not meta:
        err(where, "missing or empty NN.meta.yaml")
        return

    is_java = language == "java"
    if is_java and not (JAVAC and JAVA):
        global _jdk_warned
        if not _jdk_warned:
            warn(where, "no javac/java on PATH — Java compile + solvability checks skipped")
            _jdk_warned = True

    title = meta.get("title")
    if not (isinstance(title, dict) and title.get("en") and title.get("zh")):
        err(where, "title must have both 'en' and 'zh'")

    # Parallel step counts + compile example fences (python or java per course).
    counts: dict[str, int] = {}
    fence_re = JAVA_FENCE_RE if is_java else PY_FENCE_RE
    for lang in ("en", "zh"):
        p = cdir / f"{nn}.{lang}.md"
        if not p.is_file():
            err(where, f"missing {nn}.{lang}.md")
            continue
        text = p.read_text(encoding="utf-8")
        counts[lang] = len([c for c in SPLIT_RE.split(text) if c.strip()])
        for i, block in enumerate(fence_re.findall(text)):
            label = f"{lang}.md {language} block #{i + 1}"
            if is_java:
                java_compile_ok(where, label, block)
            else:
                compile_ok(where, label, block)
    if "en" in counts and "zh" in counts and counts["en"] != counts["zh"]:
        err(where, f"en/zh step count differs: en={counts['en']} zh={counts['zh']}")

    # Tasks.
    valid_kinds = ("stdout", "harness") if is_java else ("stdout", "asserts")
    for task in meta.get("tasks") or []:
        tid = task.get("id")
        tw = f"{where} task '{tid}'"
        if not tid:
            err(where, "a task is missing 'id'")
            continue
        prompt = task.get("prompt")
        if not (isinstance(prompt, dict) and prompt.get("en") and prompt.get("zh")):
            err(tw, "prompt must have both 'en' and 'zh'")
        chk = task.get("check")
        if not isinstance(chk, dict) or chk.get("kind") not in valid_kinds:
            err(tw, f"check.kind must be one of {valid_kinds}")
            chk = {}
        if chk.get("kind") == "stdout" and chk.get("expected") is None:
            err(tw, "stdout check needs 'expected'")
        if chk.get("kind") == "asserts":
            if not chk.get("code"):
                err(tw, "asserts check needs 'code'")
            else:
                compile_ok(tw, "check.code", chk["code"])
        if chk.get("kind") == "harness" and not chk.get("code"):
            err(tw, "harness check needs 'code'")

        if is_java:
            if task.get("starter"):
                # Starters may legitimately have a fill-in hole → warn, don't fail.
                java_compile_ok(tw, "starter", task["starter"], severity="warn")
            if task.get("solution"):
                run_solution_java(tw, task)   # compiles the solution too
            else:
                warn(tw, "no 'solution' — solvability gate skipped")
        else:
            if task.get("starter"):
                compile_ok(tw, "starter", task["starter"])
            if task.get("solution"):
                compile_ok(tw, "solution", task["solution"])
                run_solution(tw, task)
            else:
                warn(tw, "no 'solution' — solvability gate skipped")

    # The reference solution must never reach the browser.
    data = code_content.load_lesson(slug, nn, "en")
    if data:
        for ct in json.loads(data["tasks_client_json"]):
            if "solution" in ct:
                err(where, f"task '{ct.get('id')}' leaks 'solution' into the client JSON")


def main() -> int:
    root = code_content.CODE_DIR
    if not root.is_dir():
        print(f"No content dir: {root}")
        return 1
    courses = sorted(p.parent.name for p in root.glob("*/course.yaml"))
    if not courses:
        print("No courses found.")
        return 0

    total = 0
    for slug in courses:
        manifest = code_content._read_yaml(code_content.course_dir(slug) / "course.yaml")
        language = str(manifest.get("language") or "python")
        lessons = [str(n) for n in (manifest.get("lessons") or [])]
        print(f"Course '{slug}': {len(lessons)} lesson(s) listed [{language}]")
        for nn in lessons:
            total += 1
            check_lesson(slug, nn, language)

    print(f"\nChecked {total} lesson(s) across {len(courses)} course(s).")
    for w in warnings:
        print(" ", w)
    if errors:
        print(f"\n{len(errors)} error(s):")
        for e in errors:
            print(" ", e)
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
