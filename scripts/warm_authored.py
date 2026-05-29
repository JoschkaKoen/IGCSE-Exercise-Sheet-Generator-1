"""Orchestrate Claude-authored warming for a subject: select ~5 recent, type-diverse
papers, auto-detect MCQ vs structured, author both bank YAMLs, render snippets, and
validate — all with no paid Gemini extraction.

Per paper it dispatches to:
- ``scripts.author_mcq_paper`` when the QP is a Multiple-Choice paper, or
- ``scripts.author_structured_paper`` otherwise (theory / data-response / essay; the
  mark-scheme "Question/Answer/Marks" table is the backbone).

Then ``eXam.bank.finalize_authored_paper`` renders the per-question snippets + stamps
the cache. Validation flags mark/total mismatches, missing per-leaf scheme entries, and
papers where the snippet renderer produced nothing (its segmentation failed).

Usage::

    python -m scripts.warm_authored --subject a_level_chemistry [--want 5] [--dry-run]
    python -m scripts.warm_authored --subject igcse_business_studies --variants 11 21 31
"""

from __future__ import annotations

import argparse
import io
import contextlib
import re
import sys
from pathlib import Path

import fitz
import yaml

from eXam.bank import bank_dir_for, finalize_authored_paper
from eXam.open_mode import list_practice_papers, pair_mark_scheme
from eXam.runtime import question_metadata, mark_scheme_entry
from scripts import author_mcq_paper, author_structured_paper

# IGCSE sciences are restricted to the Extended tier (2X MC, 4X theory, 6X ATP).
_SCIENCE_FAMILIES = {"igcse_physics": {2, 4, 6}, "igcse_chemistry": {2, 4, 6},
                     "igcse_biology": {2, 4, 6}}


def _variant(name: str) -> str:
    m = re.search(r"_qp_(\d{2})", name) or re.search(r"Question [Pp]aper\s+(\d{2})", name)
    return m.group(1) if m else "00"


def _family(name: str) -> int:
    v = _variant(name)
    return int(v[0]) if v[:1].isdigit() else 0


def _session_rank(name: str) -> int:
    m = re.search(r"_([smw])\d2_", name)
    if m:
        return {"w": 0, "s": 1, "m": 2}[m.group(1)]
    return 0 if "November" in name else (1 if "June" in name else 2)


def _year(name: str) -> int:
    m = re.search(r"(20\d\d)", name) or re.search(r"_[smw](\d2)_", name)
    if not m:
        return 0
    g = m.group(1)
    return int(g) if len(g) == 4 else 2000 + int(g)


def select_papers(subject: str, want: int = 5) -> list[Path]:
    """Most-recent-first, type-diverse (round-robin by paper family)."""
    papers = [p for p in list_practice_papers(subject) if pair_mark_scheme(p)]
    allowed = _SCIENCE_FAMILIES.get(subject)
    if allowed:
        papers = [p for p in papers if _family(p.name) in allowed]
    # newest first, then by family/variant for stable round-robin
    papers.sort(key=lambda p: (-_year(p.name), _session_rank(p.name), _family(p.name), _variant(p.name)))
    by_family: dict[int, list[Path]] = {}
    for p in papers:
        by_family.setdefault(_family(p.name), []).append(p)
    fams = sorted(by_family)
    picks: list[Path] = []
    i = 0
    while len(picks) < want and any(by_family[f] for f in fams):
        f = fams[i % len(fams)]
        if by_family[f]:
            picks.append(by_family[f].pop(0))
        i += 1
    return picks[:want]


def is_mcq(qp_pdf: Path) -> bool:
    doc = fitz.open(qp_pdf)
    try:
        return "Multiple Choice" in doc[0].get_text()
    finally:
        doc.close()


def warm_one(subject: str, qp: Path, ms: Path) -> dict:
    mcq = is_mcq(qp)
    if mcq:
        eq, msd, stats = author_mcq_paper.build(subject, qp, ms)
        info = {"mode": "mcq", "n": stats["n"], "mism": 0}
    else:
        eq, msd, stats = author_structured_paper.build(subject, qp, ms)
        info = {"mode": "struct", "n": stats["tops"], "mism": len(stats["mark_mismatches"]),
                "leaves": stats["leaves"], "marks": stats["total_marks"]}
    out = bank_dir_for(subject, qp)
    out.mkdir(parents=True, exist_ok=True)
    (out / "exam_questions.yaml").write_text(
        yaml.safe_dump(eq, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (out / "mark_scheme.yaml").write_text(
        yaml.safe_dump(msd, sort_keys=False, allow_unicode=True), encoding="utf-8")
    with contextlib.redirect_stdout(io.StringIO()):
        finalize_authored_paper(qp, ms, subject)
    # validate: scheme coverage + snippet render
    n_top = info["n"]
    missing, snippets = [], 0
    for q in range(1, n_top + 1):
        meta = question_metadata(f"{subject}::{qp.stem}::{q}")
        if not meta:
            continue
        if (out / str(q) / "question.pdf").exists():
            snippets += 1
        for leaf in meta["leaves"]:
            if mark_scheme_entry(f"{subject}::{qp.stem}::{leaf['number']}") is None:
                missing.append(leaf["number"])
    info.update({"missing": missing, "snippets": snippets, "variant": _variant(qp.name)})
    return info


def main() -> int:
    ap = argparse.ArgumentParser(prog="scripts.warm_authored")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--want", type=int, default=5)
    ap.add_argument("--variants", nargs="*", help="force specific QP variants instead of auto-select")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.variants:
        papers = [p for p in list_practice_papers(args.subject)
                  if _variant(p.name) in args.variants and pair_mark_scheme(p)]
    else:
        papers = select_papers(args.subject, args.want)
    if not papers:
        print(f"no papers selected for {args.subject}", file=sys.stderr)
        return 1

    print(f"[warm] {args.subject}: {len(papers)} papers")
    bad = 0
    for qp in papers:
        ms = pair_mark_scheme(qp)
        if args.dry_run:
            print(f"   P{_variant(qp.name)}  {'MCQ' if is_mcq(qp) else 'struct':6}  {qp.name}")
            continue
        info = warm_one(args.subject, qp, ms)
        flag = ""
        if info["mism"] or info["missing"] or info["snippets"] < info["n"]:
            flag = f"  <-- mism={info['mism']} missing={len(info['missing'])} snip={info['snippets']}/{info['n']}"
            bad += 1
        extra = f"leaves={info.get('leaves','-')} marks={info.get('marks','-')}"
        print(f"   P{info['variant']:3} {info['mode']:6} q={info['n']:>2} snip={info['snippets']:>2} {extra}{flag}")
    if not args.dry_run:
        print(f"[warm] {args.subject}: {len(papers)-bad}/{len(papers)} clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
