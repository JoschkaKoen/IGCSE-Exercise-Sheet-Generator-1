"""student_names: per-student page-order check — heuristic over classify_empty_exam_pages's handwriting.json.

For every scan page, classify_empty_exam_pages's vision call already detected the printed page
number and whether the page is a cover. This step joins that data with the
per-student page_numbers from student_handwriting_check and verifies that each student's
sequence of detected page numbers matches what the empty-exam layout
expects. No OCR, no LLM call.

The dispatcher in ``xscore/steps/geometry.py`` is the single policy layer:
this module returns a :class:`PageOrderResult` and never calls SystemExit.
INCONCLUSIVE covers paths where the check could not be performed (missing
``handwriting.json``, parse error, no artifact dir). Per-page failures are
bucketed as either ``wrong_order`` (the AI gave us a signal that disagrees
with the slot) or ``missing`` (no usable signal for the slot).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xscore.shared.models import PageAssignment


class PageOrderStatus(Enum):
    PASSED = "PASSED"
    MISMATCH_FOUND = "MISMATCH_FOUND"
    INCONCLUSIVE = "INCONCLUSIVE"


class IssueKind(Enum):
    WRONG_ORDER = "wrong_order"
    MISSING = "missing"


@dataclass(frozen=True)
class Issue:
    student: str
    scan_page: int          # 1-based scan PDF page
    expected: str           # "cover" | "page N"
    detected: str           # "cover" | "page N" | "(none)"


@dataclass
class PageOrderResult:
    status: PageOrderStatus
    wrong_order: list[Issue] = field(default_factory=list)
    missing: list[Issue] = field(default_factory=list)
    passed_count: int = 0
    total_count: int = 0
    duration_s: float = 0.0
    setup_error: str | None = None   # set only when status == INCONCLUSIVE


# ─────────── Main entry ──────────────────────────────────────────────────────

def check_page_order(
    exam_pdf: Path,
    scan_pdf: Path,
    page_assignments: list["PageAssignment"],
    artifact_dir: Path | None = None,
) -> PageOrderResult:
    """Validate page order from student_handwriting_check's per-page page-number detections.

    Heuristic only — no LLM, no OCR. For each student, looks up the AI-detected
    printed page number from ``13_student_handwriting_check/handwriting.json`` for
    every scan page they own, computes the expected sequence using
    ``cover_offset`` from the same metadata block, and bins per-page failures
    into wrong-order vs missing buckets.

    ``exam_pdf`` and ``scan_pdf`` are kept in the signature for compat with
    the dispatcher; both are unused by the new implementation.
    """
    del exam_pdf, scan_pdf  # legacy params, retained for dispatcher compat

    total = len(page_assignments)

    if artifact_dir is None:
        return PageOrderResult(
            status=PageOrderStatus.INCONCLUSIVE,
            total_count=total,
            setup_error="no artifact_dir provided",
        )

    from xscore.shared.path_builders import (
        artifact_handwriting_json_path,
    )
    from xscore.shared.terminal_ui import info_line, ok_line, warn_line

    hw_path = artifact_handwriting_json_path(artifact_dir)
    if not hw_path.exists():
        result = PageOrderResult(
            status=PageOrderStatus.INCONCLUSIVE,
            total_count=total,
            setup_error=(
                "student_handwriting_check artifact not found (13_student_handwriting_check/handwriting.json); "
                "run student_handwriting_check first"
            ),
        )
        _save_issues_artifact(artifact_dir, result)
        return result
    try:
        hw_data = json.loads(hw_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        result = PageOrderResult(
            status=PageOrderStatus.INCONCLUSIVE,
            total_count=total,
            setup_error=f"could not read handwriting.json: {exc}",
        )
        _save_issues_artifact(artifact_dir, result)
        return result

    metadata = hw_data.get("metadata", {})
    cover_offset = int(metadata.get("cover_offset", 0))
    by_scan: dict[int, dict] = {
        int(entry["scan_page"]): entry
        for entry in hw_data.get("scan_pages", [])
        if entry.get("scan_page") is not None
    }

    info_line(
        f"Checking page order for {total} student"
        f"{'s' if total != 1 else ''} (heuristic) …"
    )

    name_width = max(
        (len(a.student_name or "Unknown") for a in page_assignments),
        default=1,
    )

    wrong_order_all: list[Issue] = []
    missing_all: list[Issue] = []
    passed = 0

    t_start = time.perf_counter()
    for a in page_assignments:
        has_cover = a.cover_page_number is not None
        wrong_order_st: list[Issue] = []
        missing_st: list[Issue] = []
        for p_label, scan_page in enumerate(a.page_numbers, 1):
            entry = by_scan.get(scan_page)
            # Step classify_empty_exam_pages phase B emits page_type (enum) + matched_page_number (int|None).
            page_type = entry.get("page_type") if entry is not None else None
            matched_pn = entry.get("matched_page_number") if entry is not None else None
            if has_cover and p_label == 1:
                # Cover slot: matcher should pick page_type == "cover page".
                if entry is None:
                    missing_st.append(Issue(
                        student=a.student_name, scan_page=scan_page,
                        expected="cover", detected="(none)",
                    ))
                elif page_type is not None and page_type != "cover page":
                    if matched_pn is None:
                        missing_st.append(Issue(
                            student=a.student_name, scan_page=scan_page,
                            expected="cover", detected="(none)",
                        ))
                    else:
                        wrong_order_st.append(Issue(
                            student=a.student_name, scan_page=scan_page,
                            expected="cover", detected=f"page {matched_pn}",
                        ))
                continue
            expected_pn = p_label - cover_offset
            if expected_pn < 1:
                continue  # before-first-page; nothing to check
            if entry is None:
                missing_st.append(Issue(
                    student=a.student_name, scan_page=scan_page,
                    expected=f"page {expected_pn}", detected="(none)",
                ))
                continue
            if page_type == "cover page":
                wrong_order_st.append(Issue(
                    student=a.student_name, scan_page=scan_page,
                    expected=f"page {expected_pn}", detected="cover",
                ))
                continue
            if matched_pn is None:
                missing_st.append(Issue(
                    student=a.student_name, scan_page=scan_page,
                    expected=f"page {expected_pn}", detected="(none)",
                ))
                continue
            if int(matched_pn) != expected_pn:
                wrong_order_st.append(Issue(
                    student=a.student_name, scan_page=scan_page,
                    expected=f"page {expected_pn}", detected=f"page {matched_pn}",
                ))

        nw, nm = len(wrong_order_st), len(missing_st)
        name_quoted = f"{a.student_name!r}"
        if nw == 0 and nm == 0:
            passed += 1
            ok_line(f"{name_quoted:<{name_width + 2}}  ·  page order OK")
        else:
            parts: list[str] = []
            if nw:
                parts.append(f"{nw} wrong-order")
            if nm:
                parts.append(f"{nm} missing")
            warn_line(f"{name_quoted:<{name_width + 2}}  ·  {', '.join(parts)}")
        wrong_order_all.extend(wrong_order_st)
        missing_all.extend(missing_st)

    duration_s = time.perf_counter() - t_start

    # Sort missing by (student, scan_page) so a fully-unreadable student
    # appears as a contiguous block in the table.
    missing_all.sort(key=lambda i: (i.student, i.scan_page))

    status = (
        PageOrderStatus.MISMATCH_FOUND
        if (wrong_order_all or missing_all)
        else PageOrderStatus.PASSED
    )
    result = PageOrderResult(
        status=status,
        wrong_order=wrong_order_all,
        missing=missing_all,
        passed_count=passed,
        total_count=total,
        duration_s=duration_s,
    )
    _save_issues_artifact(artifact_dir, result)
    return result


def _save_issues_artifact(artifact_dir: Path, result: PageOrderResult) -> None:
    """Write the structured result to ``<artifact_dir>/16_page_order/issues.json``."""
    from xscore.shared.path_builders import artifact_page_order_issues_path

    issues_path = artifact_page_order_issues_path(artifact_dir)
    issues_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": result.status.value,
        "wrong_order": [asdict(i) for i in result.wrong_order],
        "missing": [asdict(i) for i in result.missing],
        "passed_count": result.passed_count,
        "total_count": result.total_count,
        "duration_s": round(result.duration_s, 3),
        "setup_error": result.setup_error,
    }
    issues_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ─────────── Renderers ───────────────────────────────────────────────────────

def render_problem_tables(result: PageOrderResult) -> None:
    """Print Rich tables for each non-empty issue list."""
    if result.wrong_order:
        _render_issue_table(result.wrong_order, IssueKind.WRONG_ORDER)
    if result.missing:
        _render_issue_table(result.missing, IssueKind.MISSING)


def _render_issue_table(issues: list[Issue], kind: IssueKind) -> None:
    if not issues:
        return
    from rich import box
    from rich.padding import Padding
    from rich.table import Table

    from xscore.shared.terminal_ui import get_console

    title = (
        "Pages in wrong order"
        if kind is IssueKind.WRONG_ORDER
        else "Pages missing (no signal)"
    )

    table = Table(
        box=box.HORIZONTALS,
        header_style="dim",
        show_edge=False,
        pad_edge=False,
    )
    table.add_column("Student", justify="left")
    table.add_column("Scan page", justify="right")
    table.add_column("Expected", justify="left")
    if kind is IssueKind.WRONG_ORDER:
        table.add_column("Detected", justify="left")

    for i in issues:
        if kind is IssueKind.WRONG_ORDER:
            table.add_row(i.student, str(i.scan_page), i.expected, i.detected)
        else:
            table.add_row(i.student, str(i.scan_page), i.expected)

    console = get_console()
    console.print()
    console.print(f"    [dim]{title}[/]")
    console.print(Padding(table, (0, 0, 0, 4), expand=False))
    console.print()
