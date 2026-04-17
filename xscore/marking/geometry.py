"""Step 10 — Exam geometry: count scan pages, derive student count, write artifacts."""

from __future__ import annotations

import json
from pathlib import Path


def compute_geometry(cleaned_pdf: Path, exam_pages: int, roster: list[str]) -> dict:
    """Count scan pages, derive number of students, cross-check roster.

    Returns a dict with keys: scan_pages, exam_pages, num_students,
    num_students_roster, roster_mismatch, pages_per_student.

    Raises ValueError if scan_pages is not evenly divisible by exam_pages.
    """
    try:
        import fitz
    except ImportError:
        raise RuntimeError("PyMuPDF not installed; run: pip install pymupdf")

    with fitz.open(str(cleaned_pdf)) as doc:
        scan_pages = doc.page_count

    if exam_pages == 0:
        raise ValueError("scaffold.page_count is 0 — re-run steps 4–6 to rebuild the scaffold")
    if scan_pages % exam_pages != 0:
        raise ValueError(
            f"scan_pages={scan_pages} not divisible by exam_pages={exam_pages}. "
            "Check that the scan PDF contains complete student blocks."
        )

    num_students_scan = scan_pages // exam_pages
    roster_mismatch = num_students_scan != len(roster)

    return {
        "scan_pages": scan_pages,
        "exam_pages": exam_pages,
        "num_students": num_students_scan,
        "num_students_roster": len(roster),
        "roster_mismatch": roster_mismatch,
        "pages_per_student": exam_pages,
    }


def write_geometry_artifacts(artifact_dir: Path, geo: dict) -> None:
    """Write 10_exam_geometry.json and 10_exam_geometry.md."""
    from xscore.shared.exam_paths import artifact_geometry_json_path, artifact_geometry_md_path

    json_path = artifact_geometry_json_path(artifact_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(geo, indent=2, ensure_ascii=False), encoding="utf-8")

    mismatch_note = f" ⚠ roster has {geo['num_students_roster']}" if geo["roster_mismatch"] else ""
    md = (
        "# Exam Geometry\n\n"
        "| Field | Value |\n"
        "|-------|-------|\n"
        f"| Scan pages (total) | {geo['scan_pages']} |\n"
        f"| Pages per student (exam) | {geo['exam_pages']} |\n"
        f"| Students (scan-derived) | {geo['num_students']}{mismatch_note} |\n"
        f"| Students (roster) | {geo['num_students_roster']} |\n"
        f"| Roster mismatch | {'Yes ⚠' if geo['roster_mismatch'] else 'No'} |\n"
    )

    md_path = artifact_geometry_md_path(artifact_dir)
    md_path.write_text(md, encoding="utf-8")
