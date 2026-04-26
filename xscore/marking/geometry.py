"""Step 10 — Calculate number of scanned exam pages per student.

Pure arithmetic on page counts. Cover-page detection is performed earlier
(steps 8 and 9), so ``scan_has_cover`` is a known input by the time this
runs and ``pages_per_student`` is derived deterministically.
"""

from __future__ import annotations

import json
from pathlib import Path


def compute_geometry(
    cleaned_pdf: Path,
    exam_pages: int,
    scan_has_cover: bool,
    roster: list[str],
) -> dict:
    """Compute the scan partition.

    Returns a dict with keys: scan_pages, exam_pages, scan_has_cover,
    num_students, num_students_roster, roster_mismatch, pages_per_student.

    Raises ``ValueError`` when ``scan_pages`` is not an exact multiple of
    ``pages_per_student`` — there is no fallback divisor search. The caller
    catches and surfaces the error to the user.

    Roster mismatch is reported via the returned dict but is not fatal; the
    scan-derived count is the source of truth.
    """
    try:
        import fitz
    except ImportError:
        raise RuntimeError("PyMuPDF not installed; run: pip install pymupdf")

    with fitz.open(str(cleaned_pdf)) as doc:
        scan_pages = doc.page_count

    if exam_pages == 0:
        raise ValueError("Empty exam page count is 0 — check that the empty exam PDF exists")

    pages_per_student = exam_pages + (1 if scan_has_cover else 0)

    if scan_pages % pages_per_student != 0:
        expected_n = max(round(scan_pages / pages_per_student), 1)
        expected_total = expected_n * pages_per_student
        diff = scan_pages - expected_total
        raise ValueError(
            "Scan page count mismatch — cannot mark reliably.\n\n"
            f"  Empty exam:          {exam_pages} pages per student\n"
            f"  Scan has cover page: {'yes' if scan_has_cover else 'no'}\n"
            f"  Pages per student:   {pages_per_student}\n"
            f"  Scan pages:          {scan_pages}\n"
            f"  Closest match:       {expected_n} students × {pages_per_student} pages "
            f"= {expected_total} ({diff:+d})\n\n"
            "  Re-scan the missing/extra page(s) and re-run."
        )

    num_students_scan = scan_pages // pages_per_student
    roster_mismatch = num_students_scan != len(roster)

    return {
        "scan_pages": scan_pages,
        "exam_pages": exam_pages,
        "scan_has_cover": scan_has_cover,
        "num_students": num_students_scan,
        "num_students_roster": len(roster),
        "roster_mismatch": roster_mismatch,
        "pages_per_student": pages_per_student,
    }


def write_geometry_artifacts(artifact_dir: Path, geo: dict) -> None:
    """Write exam_geometry.json and exam_geometry.md under the geometry folder."""
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
        f"| Empty exam pages | {geo['exam_pages']} |\n"
        f"| Scan has cover page | {'Yes' if geo['scan_has_cover'] else 'No'} |\n"
        f"| Pages per student | {geo['pages_per_student']} |\n"
        f"| Students (scan-derived) | {geo['num_students']}{mismatch_note} |\n"
        f"| Students (roster) | {geo['num_students_roster']} |\n"
        f"| Roster mismatch | {'Yes ⚠' if geo['roster_mismatch'] else 'No'} |\n"
    )

    md_path = artifact_geometry_md_path(artifact_dir)
    md_path.write_text(md, encoding="utf-8")
