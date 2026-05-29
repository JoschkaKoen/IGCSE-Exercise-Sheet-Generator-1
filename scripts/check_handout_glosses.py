# -*- coding: utf-8 -*-
"""Verify inline Chinese glosses in handout markdown.

For each ``NN.md`` that has a sibling ``NN.glossary.tsv`` under
``output/eXam/handouts/<subject>/``, check:

  1. No CJK characters fall inside math (`$…$`, `$$…$$`), inline code, or
     fenced code blocks — glosses belong in prose only.
  2. Every glossary term's Chinese appears exactly once in the handout
     (the first-occurrence-per-file rule). Nested terms are handled by
     greedy longest-match tiling, so 不确定度 inside 绝对不确定度 is not
     miscounted.
  3. No stray CJK that doesn't correspond to a glossary entry.

Usage::

    .venv/bin/python -m scripts.check_handout_glosses a_level_physics        # all NN.md in subject
    .venv/bin/python -m scripts.check_handout_glosses a_level_physics 1      # one topic
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HANDOUTS_ROOT = REPO_ROOT / "output" / "eXam" / "handouts"

_CJK = re.compile(r"[一-鿿]")
_PROTECTED_PATTERNS = [
    re.compile(r"```.*?```", re.DOTALL),   # fenced code
    re.compile(r"\$\$.*?\$\$", re.DOTALL),  # display math
    re.compile(r"`[^`]*`"),                  # inline code
    re.compile(r"\$[^$\n]*\$"),              # inline math
]


def _protected_mask(text: str) -> list[bool]:
    mask = [False] * len(text)
    for pat in _PROTECTED_PATTERNS:
        for m in pat.finditer(text):
            for i in range(m.start(), m.end()):
                mask[i] = True
    return mask


def _load_glossary(path: Path) -> list[tuple[str, str]]:
    """Return [(english, chinese), …] from a TSV (skips a header row if present)."""
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        eng, zh = parts[0].strip(), parts[1].strip()
        if eng.lower() == "english":  # header
            continue
        rows.append((eng, zh))
    return rows


def _tile(text: str, zh_terms: list[str]) -> tuple[dict[str, int], list[bool]]:
    """Greedy longest-match tiling over the whole text. At every position, try
    to match the longest glossary Chinese string; record a hit and mark those
    characters as matched. Matching at every position (not only CJK positions)
    handles terms whose Chinese begins with a non-CJK char, e.g. ``X射线``.

    Returns (per-term counts, matched-char mask)."""
    by_len = sorted(set(zh_terms), key=len, reverse=True)
    counts = {z: 0 for z in zh_terms}
    matched = [False] * len(text)
    i, n = 0, len(text)
    while i < n:
        hit = next((z for z in by_len if text.startswith(z, i)), None)
        if hit is not None:
            counts[hit] = counts.get(hit, 0) + 1
            for j in range(i, i + len(hit)):
                matched[j] = True
            i += len(hit)
            continue
        i += 1
    return counts, matched


def check_file(md_path: Path, gloss_path: Path) -> list[str]:
    errors: list[str] = []
    text = md_path.read_text(encoding="utf-8")
    glossary = _load_glossary(gloss_path)
    zh_terms = [zh for _, zh in glossary]

    # 1. CJK must not appear inside protected spans.
    mask = _protected_mask(text)
    for m in _CJK.finditer(text):
        if mask[m.start()]:
            line = text.count("\n", 0, m.start()) + 1
            errors.append(f"CJK char {m.group()!r} inside math/code at line {line}")

    # 2. Each glossary term glossed exactly once. 3. No stray CJK.
    counts, matched = _tile(text, zh_terms)
    for eng, zh in glossary:
        c = counts.get(zh, 0)
        if c == 0:
            errors.append(f"glossary term {eng!r} ({zh}) never appears in handout")
        elif c > 1:
            errors.append(f"glossary term {eng!r} ({zh}) appears {c}× (first-occurrence rule)")

    stray_lines: set[int] = set()
    for m in _CJK.finditer(text):
        if not matched[m.start()]:
            stray_lines.add(text.count("\n", 0, m.start()) + 1)
    if stray_lines:
        errors.append(f"stray CJK not in glossary at line(s): {sorted(stray_lines)}")

    return errors


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    subject = argv[0]
    topic = argv[1] if len(argv) > 1 else None
    subj_dir = HANDOUTS_ROOT / subject
    if not subj_dir.is_dir():
        print(f"no such subject dir: {subj_dir}", file=sys.stderr)
        return 2

    if topic is not None:
        stems = [f"{int(topic):02d}"]
    else:
        stems = sorted(p.stem for p in subj_dir.glob("[0-9][0-9].md"))

    total_errors = 0
    checked = 0
    for stem in stems:
        md = subj_dir / f"{stem}.md"
        gloss = subj_dir / f"{stem}.glossary.tsv"
        if not md.is_file() or not gloss.is_file():
            continue
        checked += 1
        errs = check_file(md, gloss)
        if errs:
            total_errors += len(errs)
            print(f"✗ {subject}/{stem}.md  ({len(errs)} issue(s))")
            for e in errs:
                print(f"    - {e}")
        else:
            print(f"✓ {subject}/{stem}.md")
    print(f"\nChecked {checked} handout(s); {total_errors} issue(s).")
    return 1 if total_errors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
