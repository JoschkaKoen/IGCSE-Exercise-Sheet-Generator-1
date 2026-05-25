"""Calibration runner for the writing-area detector.

For each chosen paper, parses the exam PDF and writes a colour-coded overlay PDF
to ``output/calibration/<subject>__<paper_stem>__overlay.pdf``.  Used by the
calibration loop in plan §2: run, look at each overlay, judge whether boxes hit
slots, tune ``ParserConfig`` knobs, re-run.

Usage::

    .venv/bin/python scripts/calibrate_writing_areas.py
    .venv/bin/python scripts/calibrate_writing_areas.py --only mathematics physics
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

# Repo root on sys.path so we can run "python scripts/...".
REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from xscore.scaffold.pdf_parser import parse_exam_pdf
from xscore.scaffold.draw_boxes_on_empty_exam import write_scaffold_boxes_pdf
from xscore.shared.models import flatten_questions


PAPERS: list[tuple[str, str]] = [
    # (subject, relative path under exams/)
    # Math 0580 papers are all written-answer; sciences 0610/0620/0625 papers 1-2 are
    # MCQ-only and 3-6 are theory/practical; pick paper 4x (extended theory) for sciences.
    ("mathematics",            "mathematics/0580 Mathematics March 2025 Question Paper  12.pdf"),
    ("mathematics_22",         "mathematics/0580 Mathematics March 2025 Question Paper  22.pdf"),
    ("physics",                "physics/0625 Physics November 2025 Question Paper  42.pdf"),
    ("biology",                "biology/0610 Biology June 2021 Question Paper  42.pdf"),
    ("biology_32",             "biology/0610 Biology June 2021 Question Paper  32.pdf"),
    ("biology_22",             "biology/0610 Biology June 2021 Question Paper  22.pdf"),
    ("biology_62",             "biology/0610 Biology June 2021 Question Paper  62.pdf"),
    ("chemistry",              "chemistry/0620 Chemistry June 2021 Question paper  42.pdf"),
    ("computer_science",       "computer_science/0478_m20_qp_22.pdf"),
    ("a_level_biology",        "a_level_biology/9700 Biology 2022 Specimen Question Paper  3.pdf"),
    ("a_level_chemistry",      "a_level_chemistry/9701 Chemistry 2022 Specimen Question Paper  3.pdf"),
    ("a_level_physics",        "a_level_physics/9702 Physics 2022 Specimen Question Paper  3.pdf"),
    ("a_level_computer_science", "a_level_computer_science/9618 Computer Science 2021 Specimen Question Paper  2.pdf"),
]


def _summarize(qs: list) -> dict[str, int]:
    counts: dict[str, int] = {"leaves": 0}
    for q in flatten_questions(qs):
        if q.subquestions:
            continue
        counts["leaves"] += 1
        for wa in q.writing_areas:
            counts[wa.kind] = counts.get(wa.kind, 0) + 1
    return counts


def run_one(subject_tag: str, rel_path: str, out_dir: Path) -> dict:
    pdf_path = REPO / "exams" / rel_path
    if not pdf_path.exists():
        return {"subject": subject_tag, "ok": False, "error": "missing", "path": str(pdf_path)}

    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as art_tmp:
        try:
            qs = parse_exam_pdf(pdf_path, exam_folder=Path(art_tmp), artifact_dir=Path(art_tmp))
        except Exception as e:  # noqa: BLE001
            return {"subject": subject_tag, "ok": False, "error": f"parse: {e}"}
        t_parse = time.monotonic() - t0

        out_path = out_dir / f"{subject_tag}__overlay.pdf"
        try:
            written, n_rects, n_pages = write_scaffold_boxes_pdf(
                pdf_path, qs, output_path=out_path, draw_exercise_outlines=False,
            )
        except Exception as e:  # noqa: BLE001
            return {"subject": subject_tag, "ok": False, "error": f"draw: {e}"}

    summary = _summarize(qs)
    return {
        "subject": subject_tag,
        "ok": True,
        "path": str(written),
        "rects": n_rects,
        "pages": n_pages,
        "parse_s": round(t_parse, 2),
        "counts": summary,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None, help="Subset of subject tags to run.")
    ap.add_argument("--out", default=str(REPO / "output" / "calibration"))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    selected = PAPERS if not args.only else [p for p in PAPERS if p[0] in args.only]
    if not selected:
        print(f"no papers match {args.only!r}; available tags:")
        for tag, _ in PAPERS:
            print(f"  {tag}")
        return 1

    results = []
    for tag, rel in selected:
        print(f">>> {tag}: {rel}", flush=True)
        r = run_one(tag, rel, out_dir)
        results.append(r)
        if r["ok"]:
            print(f"    OK  parse={r['parse_s']}s  rects={r['rects']}  pages={r['pages']}  counts={r['counts']}")
        else:
            print(f"    FAIL  {r['error']}")

    print()
    print("Summary:")
    for r in results:
        line = f"  {r['subject']:30s}"
        if r["ok"]:
            line += f"  ok  counts={r['counts']}"
        else:
            line += f"  FAIL: {r['error']}"
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
