#!/usr/bin/env python3
"""Verify that Learn-page syllabus-content bold edits ONLY added ``**`` markers.

Pure-Python, no API. For each ``syllabi/content/<subject>/*.md`` file this asserts
the hard invariant that removing every ``**`` reproduces the file's pristine content
at a baseline commit (default ``55a656d8``, the pre-bolding state) byte-for-byte, plus
structural checks that no ``**`` sits inside ``$…$`` / ``$$…$$`` math (which would break
KaTeX) or crosses a table ``|`` boundary. Also prints a progress summary (files with vs
without bold) so the rollout can be resumed.

The checker never decides *what* to bold — that is editorial judgement done by hand. It
only guards against accidental text drift and render-breaking ``**`` placement.

Usage::

    .venv/bin/python -m scripts.check_syllabus_bold                  # all subjects
    .venv/bin/python -m scripts.check_syllabus_bold a_level_physics  # one subject
    .venv/bin/python -m scripts.check_syllabus_bold --base <gitref>  # custom baseline

Exit code is non-zero if any file fails a check.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIR = REPO_ROOT / "syllabi" / "content"
DEFAULT_BASE = "55a656d8"  # pre-bolding commit; tag: syllabus-content-pristine

# Mask display math first (may span a line), then inline math.
_MATH_PATTERNS = [re.compile(r"\$\$.*?\$\$", re.S), re.compile(r"\$[^$\n]*\$")]


def strip_bold(s: str) -> str:
    """Remove every ``**`` (the only markup the rollout adds)."""
    return s.replace("**", "")


def _math_spans(s: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for pat in _MATH_PATTERNS:
        spans += [(m.start(), m.end()) for m in pat.finditer(s)]
    return spans


def check_file(path: Path, rel: str, base: str) -> list[str]:
    """Return a list of problem descriptions for one file (empty == clean)."""
    work = path.read_text(encoding="utf-8")
    res = subprocess.run(
        ["git", "show", f"{base}:{rel}"],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if res.returncode != 0:
        return [f"no baseline at {base}"]
    base_txt = res.stdout

    if strip_bold(work) != base_txt:
        # Everything else is meaningless once the underlying text has drifted.
        return ["TEXT DRIFT — de-bolded text differs from baseline"]

    problems: list[str] = []
    if work.count("**") % 2:
        problems.append("odd ** count")
    spans = _math_spans(work)
    if any(any(a <= m.start() < b for a, b in spans) for m in re.finditer(r"\*\*", work)):
        problems.append("** inside a $…$ / $$…$$ math span")
    for m in re.finditer(r"\*\*(.+?)\*\*", work, re.S):
        if "|" in m.group(1):
            problems.append("** pair crosses a table pipe")
            break
    if work.count("\n") != base_txt.count("\n"):
        problems.append("line count changed")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("subjects", nargs="*", help="subject keys (default: all under syllabi/content/)")
    ap.add_argument("--base", default=DEFAULT_BASE, help=f"baseline git ref (default {DEFAULT_BASE})")
    args = ap.parse_args()

    if not CONTENT_DIR.is_dir():
        print(f"content dir not found: {CONTENT_DIR}", file=sys.stderr)
        return 2

    subjects = args.subjects or sorted(p.name for p in CONTENT_DIR.iterdir() if p.is_dir())
    any_fail = False
    for subj in subjects:
        root = CONTENT_DIR / subj
        if not root.is_dir():
            print(f"{subj}: NOT FOUND")
            any_fail = True
            continue
        n_files = n_bold = n_terms = 0
        fails: list[tuple[str, str]] = []
        for md in sorted(root.glob("*.md")):
            n_files += 1
            txt = md.read_text(encoding="utf-8")
            if "**" in txt:
                n_bold += 1
            n_terms += txt.count("**") // 2
            for prob in check_file(md, f"syllabi/content/{subj}/{md.name}", args.base):
                fails.append((md.name, prob))
        status = "OK" if not fails else f"{len(fails)} FAIL"
        print(f"{subj}: {n_files} files, {n_bold} bolded, {n_terms} terms — {status}")
        for name, prob in fails:
            print(f"    {name}: {prob}")
        if fails:
            any_fail = True
    return 1 if any_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
