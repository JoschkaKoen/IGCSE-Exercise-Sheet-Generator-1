#!/usr/bin/env python3
"""Validate authored Code-page lessons under ``content/code/``.

For every course (a directory with ``course.yaml``) and every lesson it lists, check:

  - **Schema** — ``meta.yaml`` has a bilingual ``title``; each task has a unique
    ``id`` (progress is keyed by it), a bilingual ``prompt``, and a valid ``check``
    (``stdout`` needs ``expected``; ``asserts`` (Python) / ``harness`` (Java, C) need
    ``code``).
  - **Parallel languages** — ``NN.en.md`` and ``NN.zh.md`` exist and split into the
    SAME number of ``---`` step chunks.
  - **Syntax validity** — example fences (```` ```python ````/```` ```java ````/```` ```c ````),
    ``starter``s, ``solution``s, and ``asserts`` ``code`` compile (Python ``compile``;
    Java ``javac --release 8``; C ``gcc -fsyntax-only``).
  - **Solvability (the key gate)** — each task's reference ``solution`` passes its own
    ``check``, run in a subprocess (clean temp cwd, short timeout) that mirrors the
    browser worker (``web/static/js/code-worker.js``): ``stdout`` compares stripped
    (when ``normalize`` is ``strip``/unset); ``asserts`` execs the solution then the
    check code in one namespace; ``stdin`` is fed then EOF.
  - **Starter is incomplete** — each task's ``starter`` must NOT already pass its
    ``check`` (else the task is a no-op with nothing for the student to do). The
    same runner is reused with the starter as the source.
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
from web.c_runner import CC, CC_FLAGS, build_c_files  # noqa: E402  — shared with the C server runner
from web.sql_runner import compare_rows, render_grid, run_sql  # noqa: E402  — same SQLite engine as the browser's sql.js

PY = sys.executable
SPLIT_RE = re.compile(r"(?m)^\s*-{3,}\s*$")
PY_FENCE_RE = re.compile(r"```python\n(.*?)\n```", re.S)
JAVA_FENCE_RE = re.compile(r"```java\n(.*?)\n```", re.S)
C_FENCE_RE = re.compile(r"```c\n(.*?)\n```", re.S)
C_MAIN_RE = re.compile(r"\bmain\s*\(")
TIMEOUT = 10
# JAVAC / JAVA / JAVA_RELEASE / TYPE_DECL_RE and the file-layout + stdout-compare
# helpers are imported from web.java_runner above; CC / CC_FLAGS / build_c_files from
# web.c_runner. The validator and the server runners share these, so they can never
# drift. A missing JDK / gcc makes the corresponding checks a skipped warning.

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
_gcc_warned = False


def err(where: str, msg: str) -> None:
    errors.append(f"[FAIL] {where}: {msg}")


def warn(where: str, msg: str) -> None:
    warnings.append(f"[warn] {where}: {msg}")


def compile_ok(where: str, label: str, src: str) -> None:
    try:
        compile(src, label, "exec")
    except SyntaxError as e:
        err(where, f"{label} does not compile: {e}")


def run_solution(where: str, task: dict, *, source: str | None = None,
                 report: bool = True) -> bool:
    """Run *source* (default ``task['solution']``) through the task's check in a
    subprocess. Returns True iff it passes. With ``report=False`` a failure is
    NOT recorded as an error — used to probe that a *starter* does not already
    pass (see ``starter_must_fail``)."""
    spec = {
        "solution": task["solution"] if source is None else source,
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
            if report:
                err(where, f"solution timed out (>{TIMEOUT}s) — infinite loop?")
            return False
    if r.returncode != 0:
        if report:
            err(where, "reference solution does not pass its check:\n  " +
                (r.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))
        return False
    return True


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


def run_solution_java(where: str, task: dict, *, source: str | None = None,
                      report: bool = True) -> bool:
    """Compile *source* (default ``task['solution']``) + files (+ harness) and run
    it, applying the worker's check rules (stdout strip/normalize, harness = exit
    0). Returns True iff it passes. With ``report=False`` failures are not recorded
    — used to probe that a *starter* does not already pass (see ``starter_must_fail``)."""
    if not (JAVAC and JAVA):
        return False
    chk = task.get("check") or {}
    kind = chk.get("kind")
    code = task["solution"] if source is None else source
    files, main_class = build_files(code, task.get("files"), chk)
    with tempfile.TemporaryDirectory() as tmp:
        for name, s in files.items():
            (Path(tmp) / name).write_text(s, encoding="utf-8")
        try:
            c = subprocess.run([JAVAC, "--release", JAVA_RELEASE, *files.keys()],
                               cwd=tmp, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            if report:
                err(where, "solution javac timed out")
            return False
        if c.returncode != 0:
            if report:
                err(where, "reference solution does not compile:\n  " +
                    (c.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))
            return False
        try:
            r = subprocess.run([JAVA, "-cp", ".", main_class],
                               cwd=tmp, capture_output=True, text=True, timeout=TIMEOUT,
                               input=task.get("stdin") or "")
        except subprocess.TimeoutExpired:
            if report:
                err(where, f"solution run timed out (>{TIMEOUT}s) — infinite loop?")
            return False
        if kind == "harness":
            if r.returncode != 0:
                if report:
                    err(where, "reference solution fails its harness:\n  " +
                        (r.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))
                return False
            return True
        # stdout
        if r.returncode != 0:
            if report:
                err(where, "reference solution threw at runtime:\n  " +
                    (r.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))
            return False
        if not compare_stdout(r.stdout, chk):
            if report:
                norm = chk.get("normalize")
                got = r.stdout.strip() if norm in (None, "strip") else r.stdout
                exp = str(chk.get("expected") if chk.get("expected") is not None else "")
                exp = exp.strip() if norm in (None, "strip") else exp
                err(where, "reference solution does not pass its check:\n  got: %r\n  exp: %r" % (got, exp))
            return False
        return True


# ---- C path (compile/run helpers shared via web.c_runner) ---------------------

def c_compile_ok(where: str, label: str, src: str, severity: str = "err") -> None:
    """gcc -fsyntax-only a C snippet (CC_FLAGS, the server's exact flags). Skipped
    when gcc is absent or the source is empty. No JDK-style type gate here — the
    prose-fence caller skips fragments (only complete, main-bearing programs reach
    this); task starters/solutions are passed as-is."""
    if not CC or not (src or "").strip():
        return
    report = err if severity == "err" else warn
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "snippet.c").write_text(src, encoding="utf-8")
        try:
            r = subprocess.run([CC, *CC_FLAGS, "-fsyntax-only", "snippet.c"],
                               cwd=tmp, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            report(where, f"{label}: gcc timed out")
            return
        if r.returncode != 0:
            report(where, f"{label} does not compile:\n  " +
                   (r.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))


def run_solution_c(where: str, task: dict, *, source: str | None = None,
                   report: bool = True) -> bool:
    """Compile *source* (default ``task['solution']``) + files (+ harness) with the
    SAME gcc invocation the server uses (``CC_FLAGS`` + ``-lm`` LAST) and run it,
    applying the worker's check rules (stdout strip/normalize, harness = exit 0).
    Shares ``build_c_files`` + ``CC_FLAGS`` + ``compare_stdout`` with ``web.c_runner``
    so the validator and server can't drift. With ``report=False`` failures are not
    recorded — used to probe that a *starter* does not already pass."""
    if not CC:
        return False
    chk = task.get("check") or {}
    kind = chk.get("kind")
    code = task["solution"] if source is None else source
    files, sources = build_c_files(code, task.get("files"), chk)
    with tempfile.TemporaryDirectory() as tmp:
        for name, s in files.items():
            (Path(tmp) / name).write_text(s, encoding="utf-8")
        try:
            c = subprocess.run([CC, *CC_FLAGS, *sources, "-o", "main", "-lm"],
                               cwd=tmp, capture_output=True, text=True, timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            if report:
                err(where, "solution gcc timed out")
            return False
        if c.returncode != 0:
            if report:
                err(where, "reference solution does not compile:\n  " +
                    (c.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))
            return False
        try:
            r = subprocess.run(["./main"], cwd=tmp, capture_output=True, text=True,
                               timeout=TIMEOUT, input=task.get("stdin") or "")
        except subprocess.TimeoutExpired:
            if report:
                err(where, f"solution run timed out (>{TIMEOUT}s) — infinite loop?")
            return False
        if kind == "harness":
            if r.returncode != 0:
                if report:
                    err(where, "reference solution fails its harness:\n  " +
                        (r.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))
                return False
            return True
        # stdout
        if r.returncode != 0:
            if report:
                err(where, "reference solution threw at runtime:\n  " +
                    (r.stderr.strip().replace("\n", "\n  ") or "(no stderr)"))
            return False
        if not compare_stdout(r.stdout, chk):
            if report:
                norm = chk.get("normalize")
                got = r.stdout.strip() if norm in (None, "strip") else r.stdout
                exp = str(chk.get("expected") if chk.get("expected") is not None else "")
                exp = exp.strip() if norm in (None, "strip") else exp
                err(where, "reference solution does not pass its check:\n  got: %r\n  exp: %r" % (got, exp))
            return False
        return True


# ---- SQL path (execution + render helpers shared via web.sql_runner) ----------

def run_solution_sql(where: str, task: dict, seed: str, *, source: str | None = None,
                     report: bool = True) -> bool:
    """Run *source* (default ``task['solution']``) against the seeded in-memory DB
    via ``web.sql_runner`` (Python ``sqlite3`` — the same SQLite engine as the
    browser's sql.js), render the result grid, and compare to ``check['expected']``.
    Returns True iff it matches. With ``report=False`` failures are silent — used to
    probe that a *starter* does not already pass."""
    chk = task.get("check") or {}
    code = task["solution"] if source is None else source
    cols, rows, error = run_sql(seed, code or "", chk.get("probe"))
    if error:
        if report:
            err(where, f"reference solution raised a SQL error:\n  {error}")
        return False
    got = render_grid(cols, rows, chk.get("ordered") is not False)
    if not compare_rows(got, chk.get("expected")):
        if report:
            exp = str(chk.get("expected") if chk.get("expected") is not None else "").strip()
            err(where, "reference solution does not pass its check:\n  got: %r\n  exp: %r" % (got.strip(), exp))
        return False
    return True


def starter_must_fail(where: str, task: dict, language: str, *, seed: str = "") -> None:
    """A task whose STARTER already passes its check is a no-op — the student
    would have nothing to do. Probe the starter against the check (report=False,
    so a normal "starter fails" outcome is silent) and flag it only if it passes.
    A starter that doesn't compile or errors correctly counts as 'fails'. When the
    toolchain (JDK / gcc) is absent the probe can't run and simply skips (False)."""
    starter = task.get("starter")
    chk = task.get("check") or {}
    if not starter or not chk.get("kind"):
        return
    if language == "sql":
        passed = run_solution_sql(where, task, seed, source=starter, report=False)
    else:
        runner = run_solution_java if language == "java" else run_solution_c if language == "c" else run_solution
        passed = runner(where, task, source=starter, report=False)
    if passed:
        err(where, f"task '{task.get('id')}' starter already passes its check — "
                   "the task is a no-op; the starter must leave something to complete")


def check_lesson(slug: str, nn: str, language: str = "python") -> None:
    cdir = code_content.course_dir(slug)
    where = f"{slug}/{nn}"
    meta = code_content._read_yaml(cdir / f"{nn}.meta.yaml")
    if not meta:
        err(where, "missing or empty NN.meta.yaml")
        return

    is_java = language == "java"
    is_c = language == "c"
    is_sql = language == "sql"
    if is_java and not (JAVAC and JAVA):
        global _jdk_warned
        if not _jdk_warned:
            warn(where, "no javac/java on PATH — Java compile + solvability checks skipped")
            _jdk_warned = True
    if is_c and not CC:
        global _gcc_warned
        if not _gcc_warned:
            warn(where, "no gcc on PATH — C compile + solvability checks skipped")
            _gcc_warned = True

    title = meta.get("title")
    if not (isinstance(title, dict) and title.get("en") and title.get("zh")):
        err(where, "title must have both 'en' and 'zh'")

    # Parallel step counts + compile example fences (python / java / c per course).
    counts: dict[str, int] = {}
    fence_re = C_FENCE_RE if is_c else JAVA_FENCE_RE if is_java else PY_FENCE_RE
    for lang in ("en", "zh"):
        p = cdir / f"{nn}.{lang}.md"
        if not p.is_file():
            err(where, f"missing {nn}.{lang}.md")
            continue
        text = p.read_text(encoding="utf-8")
        counts[lang] = len([c for c in SPLIT_RE.split(text) if c.strip()])
        if is_sql:
            continue   # no cheap SQL syntax-only check; the solvability gate covers it
        for i, block in enumerate(fence_re.findall(text)):
            label = f"{lang}.md {language} block #{i + 1}"
            if is_java:
                java_compile_ok(where, label, block)
            elif is_c:
                # Only syntax-check complete (runnable) programs; an illustrative
                # fragment without main() isn't a translation unit, so skip it
                # (mirrors the Java type-decl gate in java_compile_ok).
                if C_MAIN_RE.search(block):
                    c_compile_ok(where, label, block)
            else:
                compile_ok(where, label, block)
    if "en" in counts and "zh" in counts and counts["en"] != counts["zh"]:
        err(where, f"en/zh step count differs: en={counts['en']} zh={counts['zh']}")

    # Tasks.
    if is_sql:
        valid_kinds = ("rows",)
    elif is_java or is_c:
        valid_kinds = ("stdout", "harness")
    else:
        valid_kinds = ("stdout", "asserts")
    seen_ids: set[str] = set()
    for task in meta.get("tasks") or []:
        tid = task.get("id")
        tw = f"{where} task '{tid}'"
        if not tid:
            err(where, "a task is missing 'id'")
            continue
        # Progress (code_progress table) is keyed by task id, so duplicates would
        # silently conflate two tasks' done/revealed state.
        if tid in seen_ids:
            err(where, f"duplicate task id '{tid}' in this lesson")
        seen_ids.add(tid)
        prompt = task.get("prompt")
        if not (isinstance(prompt, dict) and prompt.get("en") and prompt.get("zh")):
            err(tw, "prompt must have both 'en' and 'zh'")
        chk = task.get("check")
        if not isinstance(chk, dict) or chk.get("kind") not in valid_kinds:
            err(tw, f"check.kind must be one of {valid_kinds}")
            chk = {}
        if chk.get("kind") == "stdout" and chk.get("expected") is None:
            err(tw, "stdout check needs 'expected'")
        if chk.get("kind") == "rows" and chk.get("expected") is None:
            err(tw, "rows check needs 'expected'")
        if chk.get("kind") == "asserts":
            if not chk.get("code"):
                err(tw, "asserts check needs 'code'")
            else:
                compile_ok(tw, "check.code", chk["code"])
        if chk.get("kind") == "harness" and not chk.get("code"):
            err(tw, "harness check needs 'code'")

        # SQL seed (CREATE+INSERT run before the student's code); "" for non-SQL tasks.
        seed = code_content.resolve_seed(slug, meta, task)

        if is_java:
            if task.get("starter"):
                # Starters may legitimately have a fill-in hole → warn, don't fail.
                java_compile_ok(tw, "starter", task["starter"], severity="warn")
            if task.get("solution"):
                run_solution_java(tw, task)   # compiles the solution too
            else:
                warn(tw, "no 'solution' — solvability gate skipped")
        elif is_c:
            if task.get("starter"):
                # Starters may legitimately have a fill-in hole → warn, don't fail.
                c_compile_ok(tw, "starter", task["starter"], severity="warn")
            if task.get("solution"):
                run_solution_c(tw, task)   # compiles the solution too
            else:
                warn(tw, "no 'solution' — solvability gate skipped")
        elif is_sql:
            if task.get("solution"):
                run_solution_sql(tw, task, seed)
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

        # A well-formed task's starter must NOT already pass — else it teaches
        # nothing. (Independent of whether a reference solution is present.)
        starter_must_fail(tw, task, language, seed=seed)

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
