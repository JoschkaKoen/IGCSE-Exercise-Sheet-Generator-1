"""Step 16: per-student page-order check — heuristic over step 14's handwriting.json.

For every scan page, step 14's vision call already detected the printed page
number and whether the page is a cover. This step joins that data with the
per-student page_numbers from step 15 and verifies that each student's
sequence of detected page numbers matches what the empty-exam layout
expects. No OCR, no LLM call.

The dispatcher in ``xscore/steps/geometry.py`` is the single policy layer:
this module returns ``(PageOrderStatus, message)`` and never calls SystemExit
or prints. INCONCLUSIVE covers every path that today silently fails open
(missing handwriting.json, parse error, model returned None for too many
pages to draw a conclusion).
"""

from __future__ import annotations

import json
import time
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xscore.shared.models import PageAssignment


class PageOrderStatus(Enum):
    PASSED = "PASSED"
    MISMATCH_FOUND = "MISMATCH_FOUND"
    INCONCLUSIVE = "INCONCLUSIVE"


# ─────────── Main entry ──────────────────────────────────────────────────────

def check_page_order(
    exam_pdf: Path,
    scan_pdf: Path,
    page_assignments: list["PageAssignment"],
    artifact_dir: Path | None = None,
) -> tuple[PageOrderStatus, str | None]:
    """Validate page order from step 14's per-page page-number detections.

    Heuristic only — no LLM, no OCR. For each student, looks up the AI-detected
    printed page number from ``14_student_handwriting/handwriting.json`` for
    every scan page they own, computes the expected sequence using
    ``cover_offset`` from the same metadata block, and flags students whose
    detected sequence disagrees with the expected one.

    ``exam_pdf`` and ``scan_pdf`` are kept in the signature for compat with
    the dispatcher; both are unused by the new implementation.
    """
    del exam_pdf, scan_pdf  # legacy params, retained for dispatcher compat

    if artifact_dir is None:
        return PageOrderStatus.INCONCLUSIVE, "no artifact_dir provided"

    from xscore.shared.exam_paths import artifact_handwriting_json_path
    from xscore.shared.terminal_ui import (
        format_duration,
        info_line,
        ok_line,
        warn_line,
    )

    hw_path = artifact_handwriting_json_path(artifact_dir)
    if not hw_path.exists():
        return (
            PageOrderStatus.INCONCLUSIVE,
            "step 14 artifact not found (14_student_handwriting/handwriting.json); "
            "run student_handwriting_check first",
        )
    try:
        hw_data = json.loads(hw_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return PageOrderStatus.INCONCLUSIVE, f"could not read handwriting.json: {exc}"

    metadata = hw_data.get("metadata", {})
    cover_offset = int(metadata.get("cover_offset", 0))
    by_scan: dict[int, dict] = {
        int(entry["scan_page"]): entry
        for entry in hw_data.get("scan_pages", [])
        if entry.get("scan_page") is not None
    }

    info_line(
        f"Checking page order for {len(page_assignments)} student"
        f"{'s' if len(page_assignments) != 1 else ''} (heuristic) …"
    )

    name_width = max(
        (len(a.student_name or "Unknown") for a in page_assignments),
        default=1,
    )

    issue_total: list[dict] = []
    inconclusive: list[str] = []
    passed = 0

    t_start = time.perf_counter()
    for a in page_assignments:
        has_cover = a.cover_page_number is not None
        student_issues: list[dict] = []
        n_no_pn = 0
        for p_label, scan_page in enumerate(a.page_numbers, 1):
            entry = by_scan.get(scan_page)
            if has_cover and p_label == 1:
                # Cover page: AI should say is_cover_page=True, no printed
                # page number expected.
                if entry is not None and entry.get("is_cover_page") is False:
                    student_issues.append({
                        "scan_page": scan_page,
                        "expected": "cover",
                        "detected": (
                            f"page {entry.get('detected_page_number')}"
                            if entry.get("detected_page_number") is not None
                            else "non-cover"
                        ),
                    })
                continue
            expected_pn = p_label - cover_offset
            if expected_pn < 1:
                continue  # before-first-page; nothing to check
            if entry is None:
                n_no_pn += 1
                continue
            detected_pn = entry.get("detected_page_number")
            if detected_pn is None:
                n_no_pn += 1
                continue
            if entry.get("is_cover_page") is True:
                student_issues.append({
                    "scan_page": scan_page,
                    "expected": f"page {expected_pn}",
                    "detected": "cover",
                })
                continue
            if int(detected_pn) != expected_pn:
                student_issues.append({
                    "scan_page": scan_page,
                    "expected": f"page {expected_pn}",
                    "detected": f"page {detected_pn}",
                })

        name_quoted = f"{a.student_name!r}"
        if student_issues:
            issue_total.extend({"student": a.student_name, **i} for i in student_issues)
            warn_line(
                f"{name_quoted:<{name_width + 2}}  ·  "
                f"page order MISMATCH ({len(student_issues)} issue"
                f"{'s' if len(student_issues) != 1 else ''})"
            )
        elif n_no_pn > 0 and n_no_pn >= len(a.page_numbers) - (1 if has_cover else 0):
            inconclusive.append(a.student_name)
            warn_line(
                f"{name_quoted:<{name_width + 2}}  ·  "
                f"inconclusive: AI returned no page number on {n_no_pn} pages"
            )
        else:
            passed += 1
            ok_line(f"{name_quoted:<{name_width + 2}}  ·  page order OK")

    dur = format_duration(time.perf_counter() - t_start)

    if issue_total:
        sample = "; ".join(
            f"{i['student']} scan {i['scan_page']}: {i['detected']} (expected {i['expected']})"
            for i in issue_total[:5]
        )
        more = (
            f" (and {len(issue_total) - 5} more)"
            if len(issue_total) > 5 else ""
        )
        return (
            PageOrderStatus.MISMATCH_FOUND,
            f"page-order mismatches detected in {dur}: {sample}{more}",
        )
    if inconclusive:
        return (
            PageOrderStatus.INCONCLUSIVE,
            f"{len(inconclusive)} student(s) had insufficient page-number detections "
            f"to verify order: {', '.join(inconclusive[:10])}",
        )
    return (
        PageOrderStatus.PASSED,
        f"{passed}/{len(page_assignments)} students — page order OK ({dur})",
    )
