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
    empty_exam_has_cover: bool | None,
    scan_has_cover: bool,
    roster: list[str],
) -> dict:
    """Compute the scan partition.

    Returns a dict with keys: scan_pages, exam_pages, empty_exam_has_cover,
    scan_has_cover, num_students, num_students_roster, roster_mismatch,
    pages_per_student, mismatch_warning.

    The cover offset matches the convention used elsewhere in the codebase
    (see ai_mark.py, blank_page_detection.py): the scan adds a cover only
    when ``scan_has_cover`` is True AND the empty exam does not already
    include its own cover.

    When ``scan_pages`` is not an exact multiple of ``pages_per_student``,
    falls back to ``round(scan_pages / pages_per_student)`` for
    ``num_students`` and records the discrepancy in ``mismatch_warning``
    (a multi-line string) so downstream steps can run and pinpoint where
    the misalignment surfaces. ``mismatch_warning`` is ``None`` on a clean
    match. The caller is responsible for surfacing the warning and
    (optionally) fail-fast.

    Raises ``ValueError`` only for inputs we cannot recover from:
    ``exam_pages == 0``, or ``empty_exam_has_cover is None`` while
    ``scan_has_cover is True`` (step 8 skipped but the scan has a cover —
    we cannot safely decide whether the empty exam already counts it).

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

    if empty_exam_has_cover is None and scan_has_cover:
        raise ValueError(
            "empty_exam_has_cover was not determined (step 8 skipped?) — "
            "cannot safely compute pages_per_student when the scan has cover pages"
        )

    cover_offset = 1 if (scan_has_cover and not empty_exam_has_cover) else 0
    pages_per_student = exam_pages + cover_offset

    if scan_pages % pages_per_student != 0:
        expected_n = max(round(scan_pages / pages_per_student), 1)
        expected_total = expected_n * pages_per_student
        diff = scan_pages - expected_total
        empty_str = (
            "yes" if empty_exam_has_cover else
            "no" if empty_exam_has_cover is False else
            "unknown"
        )
        mismatch_warning = (
            "Scan page count mismatch — proceeding with closest match.\n\n"
            f"  Empty exam:                {exam_pages} pages\n"
            f"  Empty exam has cover page: {empty_str}\n"
            f"  Scan has cover page:       {'yes' if scan_has_cover else 'no'}\n"
            f"  Pages per student:         {pages_per_student}\n"
            f"  Scan pages:                {scan_pages}\n"
            f"  Closest match:             {expected_n} students × {pages_per_student} pages "
            f"= {expected_total} ({diff:+d})\n\n"
            "  Continuing so downstream steps can pinpoint the misalignment.\n"
            "  Set GEOMETRY_STRICT=1 to fail-fast on this check."
        )
        num_students_scan = expected_n
    else:
        mismatch_warning = None
        num_students_scan = scan_pages // pages_per_student

    roster_mismatch = num_students_scan != len(roster)

    return {
        "scan_pages": scan_pages,
        "exam_pages": exam_pages,
        "empty_exam_has_cover": empty_exam_has_cover,
        "scan_has_cover": scan_has_cover,
        "num_students": num_students_scan,
        "num_students_roster": len(roster),
        "roster_mismatch": roster_mismatch,
        "pages_per_student": pages_per_student,
        "mismatch_warning": mismatch_warning,
    }


def write_geometry_artifacts(artifact_dir: Path, geo: dict) -> None:
    """Write exam_geometry.json and exam_geometry.md under the geometry folder."""
    from xscore.shared.exam_paths import artifact_geometry_json_path, artifact_geometry_md_path

    json_path = artifact_geometry_json_path(artifact_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(geo, indent=2, ensure_ascii=False), encoding="utf-8")

    mismatch_note = f" ⚠ roster has {geo['num_students_roster']}" if geo["roster_mismatch"] else ""
    empty_cover = geo.get("empty_exam_has_cover")
    empty_cover_str = (
        "Yes" if empty_cover else
        "No" if empty_cover is False else
        "Unknown"
    )
    md = (
        "# Exam Geometry\n\n"
        "| Field | Value |\n"
        "|-------|-------|\n"
        f"| Scan pages (total) | {geo['scan_pages']} |\n"
        f"| Empty exam pages | {geo['exam_pages']} |\n"
        f"| Empty exam has cover page | {empty_cover_str} |\n"
        f"| Scan has cover page | {'Yes' if geo['scan_has_cover'] else 'No'} |\n"
        f"| Pages per student | {geo['pages_per_student']} |\n"
        f"| Students (scan-derived) | {geo['num_students']}{mismatch_note} |\n"
        f"| Students (roster) | {geo['num_students_roster']} |\n"
        f"| Roster mismatch | {'Yes ⚠' if geo['roster_mismatch'] else 'No'} |\n"
    )

    md_path = artifact_geometry_md_path(artifact_dir)
    md_path.write_text(md, encoding="utf-8")
