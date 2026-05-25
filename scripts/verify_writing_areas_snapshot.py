"""Snapshot regression test for the writing-area detector.

Runs ``parse_exam_pdf`` against every calibration paper in
:data:`xscore.shared.calibration_papers.PAPERS` and asserts that the resulting
``WritingArea`` regions match committed golden JSON files under
``regression_snapshots/writing_areas/``.

Default mode: compare against goldens, exit non-zero on any mismatch.

``--update-snapshots`` regenerates the goldens.  Reserve this for **intentional**
calibration improvements landed in a separate commit — never use it to paper
over a refactor regression.

Usage::

    .venv/bin/python scripts/verify_writing_areas_snapshot.py
    .venv/bin/python scripts/verify_writing_areas_snapshot.py --update-snapshots
    .venv/bin/python scripts/verify_writing_areas_snapshot.py --only mathematics biology_32

A run takes ~60 seconds across all 13 papers (1-9 seconds each, no AI calls).
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from xscore.scaffold.pdf_parser import parse_exam_pdf
from xscore.shared.calibration_papers import PAPERS
from xscore.shared.models import Question, flatten_questions


SNAPSHOT_DIR = REPO / "regression_snapshots" / "writing_areas"


def _snapshot_for_paper(subject_tag: str, pdf_path: Path) -> dict:
    """Run the detector against *pdf_path* and serialize its output deterministically.

    Walks every leaf (`q.subquestions` empty) and emits its writing areas in a
    stable sort order so the JSON diffs cleanly on regression.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        qs = parse_exam_pdf(pdf_path, exam_folder=tmpdir, artifact_dir=tmpdir)

    leaves: list[dict] = []
    for q in flatten_questions(qs):
        if q.subquestions:
            continue
        regions = [
            {
                "kind": wa.kind,
                "page": int(wa.bbox.page),
                "x0": round(float(wa.bbox.x0), 1),
                "y0": round(float(wa.bbox.y0), 1),
                "x1": round(float(wa.bbox.x1), 1),
                "y1": round(float(wa.bbox.y1), 1),
            }
            for wa in q.writing_areas
        ]
        regions.sort(key=lambda r: (r["page"], r["y0"], r["x0"], r["kind"]))
        leaves.append({"q_number": q.number, "writing_areas": regions})

    leaves.sort(key=lambda lf: lf["q_number"])
    return {"paper": subject_tag, "leaves": leaves}


def _serialize(payload: dict) -> str:
    """Render the snapshot to a stable JSON string suitable for byte-equality compare."""
    return json.dumps(payload, indent=2, sort_keys=False) + "\n"


def _golden_path(subject_tag: str) -> Path:
    return SNAPSHOT_DIR / f"{subject_tag}.json"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--update-snapshots",
        action="store_true",
        help="Regenerate goldens instead of comparing against them.",
    )
    ap.add_argument(
        "--only",
        nargs="*",
        default=None,
        help="Optional subset of subject tags to run.",
    )
    args = ap.parse_args()

    selected = PAPERS if not args.only else [p for p in PAPERS if p[0] in args.only]
    if not selected:
        print(f"no papers match {args.only!r}; available tags:")
        for tag, _ in PAPERS:
            print(f"  {tag}")
        return 1

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    failures: list[tuple[str, str]] = []  # (tag, reason)
    skipped: list[str] = []
    updated: list[str] = []
    ok: list[str] = []

    for subject_tag, rel_path in selected:
        pdf_path = REPO / "exams" / rel_path
        if not pdf_path.exists():
            print(f"[skip] {subject_tag}: missing PDF ({rel_path})")
            skipped.append(subject_tag)
            continue

        print(f">>> {subject_tag}: parsing…", flush=True)
        payload = _snapshot_for_paper(subject_tag, pdf_path)
        rendered = _serialize(payload)
        golden = _golden_path(subject_tag)

        if args.update_snapshots:
            golden.write_text(rendered)
            print(f"    [updated] {golden.relative_to(REPO)}")
            updated.append(subject_tag)
            continue

        if not golden.exists():
            print(f"    [FAIL] {subject_tag}: no golden at {golden.relative_to(REPO)}")
            print(f"           run with --update-snapshots to create it.")
            failures.append((subject_tag, "missing golden"))
            continue

        expected = golden.read_text()
        if rendered == expected:
            print(f"    [ok] {subject_tag}")
            ok.append(subject_tag)
        else:
            # Surface enough info that the user can `git diff` quickly.
            print(f"    [FAIL] {subject_tag}: snapshot mismatch")
            print(f"           golden:  {golden.relative_to(REPO)}")
            print(f"           inspect: git diff {golden.relative_to(REPO)}")
            print(f"           (to overwrite intentionally: --update-snapshots)")
            failures.append((subject_tag, "diff"))

    print()
    print("Summary:")
    print(f"  ok:       {len(ok)}")
    print(f"  updated:  {len(updated)}")
    print(f"  skipped:  {len(skipped)} ({', '.join(skipped) if skipped else '-'})")
    print(f"  failures: {len(failures)}")
    for tag, reason in failures:
        print(f"    {tag}: {reason}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
