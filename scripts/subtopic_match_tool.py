# -*- coding: utf-8 -*-
"""Hand-match exam questions to syllabus subtopics — Claude's own classification, no API.

Replaces the Gemini call in :mod:`web.subtopic_matcher`: ``dump`` prints the syllabus
catalogue + each leaf question, I read and decide the code(s), and ``write`` produces the
same ``subtopic_matches.yaml`` sidecars the handout pipeline expects. ``status`` reports
what is still outstanding, to drive autonomous resume.

Usage::

    python -m scripts.subtopic_match_tool dump   <subject> [--paper-index I] [--unmatched-only]
    python -m scripts.subtopic_match_tool write  <subject> --paper-index I <assignments.tsv> [--force]
    python -m scripts.subtopic_match_tool status [<subject>]

``assignments.tsv``: one row per leaf, ``key<TAB>code1,code2`` (empty / ``-`` codes → no
match ``[]``). Keys and paper come from the matching ``dump --paper-index`` run.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

from eXam.bank import bank_dir_for
from web import extracted_questions
from web.handouts_collect import enumerate_topics, md_path
from web.subtopic_matcher import (
    _load_sidecar,
    _sidecar_path,
    _write_sidecar,
    build_catalogue,
    build_user_message,
    iter_leaves,
)
from web.syllabus_topics import load_topics
from xscore.shared.qnum_utils import norm_qnum

MODEL_TAG = "claude"

# Fixed authoring order (matches the approved plan).
ALL_SUBJECTS = [
    "igcse_physics", "igcse_computer_science", "igcse_chemistry", "a_level_chemistry",
    "igcse_biology", "a_level_biology", "igcse_mathematics",
    "igcse_business_studies", "igcse_economics", "a_level_business", "a_level_economics",
]


# ── helpers ───────────────────────────────────────────────────────────────


def _papers(subject: str) -> list[str]:
    """Stable, sorted paper list so --paper-index is reproducible across dump/write."""
    return sorted(extracted_questions.list_papers(subject))


def _code_key(c: str):
    return tuple((0, int(x)) if x.isdigit() else (1, x) for x in c.split("."))


def _natkey(s: str):
    return [(0, int(x)) if x.isdigit() else (1, x) for x in re.split(r"(\d+)", s) if x]


def augmented_codes(subject: str) -> tuple[str, set[str], int]:
    """``build_catalogue`` codes + a bare top-level ``N`` for subtopic-less topics.

    Some topics (e.g. IGCSE CS 7/9/10) have no ``N.M`` subtopics, so the catalogue emits
    no code for them; ``collect_questions_for_topic`` still accepts a bare ``N``.
    """
    cat, codes, subsub = build_catalogue(subject)
    data = load_topics(subject) or {}
    for t in data.get("topics") or []:
        if not (t.get("subtopics") or []):
            n = str(t.get("number") or "").strip()
            if n:
                codes.add(n)
    return cat, codes, subsub


def leaves(subject: str, paper_stem: str) -> list[tuple[str, dict, str]]:
    """``(key, q, parent_text)`` for non-STUB leaves, unique by ``norm_qnum`` (first wins)."""
    data = extracted_questions.load_paper(subject, paper_stem)
    if not data:
        return []
    out: list[tuple[str, dict, str]] = []
    seen: set[str] = set()
    for q, par in iter_leaves(data.get("questions") or []):
        if (q.get("text") or "").strip() == "STUB ERROR":
            continue
        key = norm_qnum(str(q.get("number") or ""))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((key, q, par))
    return out


def mark_scheme(subject: str, paper_stem: str) -> dict[str, str]:
    """{norm_qnum: mark_scheme_answer} — the fallback signal when question text is empty.

    Many warmed papers have only question structure (empty ``text``); the mark scheme
    answer reveals the topic (and for essay subjects even restates the question).
    """
    path = bank_dir_for(subject, Path(paper_stem)) / "mark_scheme.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    out: dict[str, str] = {}
    for q in data.get("questions") or []:
        k = norm_qnum(str(q.get("number") or ""))
        ans = (q.get("mark_scheme_answer") or "").strip()
        if k and ans:
            out[k] = ans
    return out


def matched_by_me(subject: str, paper_stem: str, leaf_keys: set[str]) -> bool:
    """True iff a sidecar exists, was written by me, and covers every leaf key."""
    path = _sidecar_path(subject, paper_stem)
    if not path.is_file():
        return False
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return False
    if data.get("model") != MODEL_TAG:
        return False
    have = {str(k) for k in (data.get("matches") or {})}
    return leaf_keys.issubset(have)


# ── commands ──────────────────────────────────────────────────────────────


def cmd_dump(args) -> int:
    subject = args.subject
    cat, codes, _ = augmented_codes(subject)
    papers = _papers(subject)

    if args.paper_index is None:
        print(f"# {subject}: {len(codes)} valid codes, {len(papers)} papers")
        print("VALID CODES: " + ", ".join(sorted(codes, key=_code_key)))
        print()
        for i, p in enumerate(papers):
            keys = {k for k, _, _ in leaves(subject, p)}
            done = matched_by_me(subject, p, keys)
            if args.unmatched_only and done:
                continue
            print(f"[{i}] {'DONE' if done else 'TODO'}  {len(keys):3} leaves  {p}")
        return 0

    i = args.paper_index
    if not (0 <= i < len(papers)):
        print(f"paper-index {i} out of range 0..{len(papers) - 1}", file=sys.stderr)
        return 2
    p = papers[i]
    lv = leaves(subject, p)
    headings = [ln for ln in cat.splitlines() if ln.startswith("## ") or ln.startswith("### ")]
    print(f"# {subject}  paper-index {i}  ({len(lv)} leaves)")
    print(f"# PAPER: {p}")
    print("# CATALOGUE (code → title):")
    for ln in headings:
        print("#   " + ln.lstrip("# "))
    print("# Assign each Q below the most specific code(s); rows: key<TAB>codes")
    print("# (MS = mark-scheme answer, used when question text wasn't extracted)")
    ms = mark_scheme(subject, p)
    if args.brief:
        for key, q, par in lv:
            t = " ".join((q.get("text") or "").split())
            pp = " ".join(par.split())
            a = " ".join(ms.get(key, "").split())
            line = f"{key}\t{q.get('question_type') or '?'}"
            if pp:
                line += f"\tPARENT: {pp[:140]}"
            if t:
                line += f"\tQ: {t[:240]}"
            if a:
                line += f"\tMS: {a[:300 if not t else 110]}"
            print(line)
    else:
        for key, q, par in lv:
            print(f"\n@@@ Q {key}")
            print(build_user_message(q, par))
            a = ms.get(key)
            if a:
                print(f"Mark scheme answer:\n{a[:400]}")
    return 0


def cmd_write(args) -> int:
    subject = args.subject
    papers = _papers(subject)
    i = args.paper_index
    if not (0 <= i < len(papers)):
        print(f"paper-index {i} out of range 0..{len(papers) - 1}", file=sys.stderr)
        return 2
    p = papers[i]
    leaf_keys = {k for k, _, _ in leaves(subject, p)}
    _, codes, _ = augmented_codes(subject)

    new: dict[str, list[str]] = {}
    errs: list[str] = []
    for ln in Path(args.assignments).read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        parts = ln.split("\t")
        key = parts[0].strip()
        raw = parts[1].strip() if len(parts) > 1 else ""
        if key.lower() == "key":
            continue
        if key not in leaf_keys:
            errs.append(f"unknown key {key!r} (not a leaf of this paper)")
            continue
        if raw in ("", "-", "[]"):
            new[key] = []
            continue
        cs = [c.strip() for c in raw.split(",") if c.strip()]
        bad = [c for c in cs if c not in codes]
        if bad:
            errs.append(f"key {key}: invalid code(s) {bad}")
            continue
        new[key] = cs

    if errs:
        for e in errs:
            print("  ERROR " + e, file=sys.stderr)
        print(f"{len(errs)} error(s); nothing written.", file=sys.stderr)
        return 1

    sidecar = _sidecar_path(subject, p)
    matches = {} if args.force else _load_sidecar(sidecar)
    matches.update(new)
    _write_sidecar(sidecar, subject=subject, paper_stem=p, model=MODEL_TAG, matches=matches)

    empty = sum(1 for v in new.values() if not v)
    missing = sorted(leaf_keys - set(matches), key=_natkey)
    tail = f"; STILL MISSING {missing}" if missing else "; complete"
    print(f"{subject} [{i}] {p}: wrote {len(new)} (empty {empty}); "
          f"sidecar {len(matches)}/{len(leaf_keys)} leaves{tail}")
    return 0


def cmd_status(args) -> int:
    subjects = [args.subject] if args.subject else ALL_SUBJECTS
    print(f"{'subject':28} {'papers me/all':>14} {'topics md/all':>14}")
    for s in subjects:
        papers = _papers(s)
        done = sum(1 for p in papers if matched_by_me(s, p, {k for k, _, _ in leaves(s, p)}))
        tops = enumerate_topics(s)
        if not tops:
            print(f"{s:28} {f'{done}/{len(papers)}':>14} {'no syllabus':>14}")
            continue
        have = sum(1 for t in tops if md_path(s, t).is_file())
        print(f"{s:28} {f'{done}/{len(papers)}':>14} {f'{have}/{len(tops)}':>14}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="subtopic_match_tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dump")
    d.add_argument("subject")
    d.add_argument("--paper-index", type=int, default=None)
    d.add_argument("--unmatched-only", action="store_true")
    d.add_argument("--brief", action="store_true")
    w = sub.add_parser("write")
    w.add_argument("subject")
    w.add_argument("--paper-index", type=int, required=True)
    w.add_argument("assignments")
    w.add_argument("--force", action="store_true")
    st = sub.add_parser("status")
    st.add_argument("subject", nargs="?", default=None)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "dump":
            return cmd_dump(args)
        if args.cmd == "write":
            return cmd_write(args)
        if args.cmd == "status":
            return cmd_status(args)
    except RuntimeError as exc:  # e.g. build_catalogue with no topics.yaml yet
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    try:
        from eXercise.env_load import load_project_env
        load_project_env()
    except Exception:
        pass
    raise SystemExit(main())
