"""Steps 25–29: per-student reports, class stats/curve, per-student PDFs,
class report, review queue.

Each step is a thin wrapper around the corresponding ``step_NN_*`` function
in :mod:`xscore.marking.merge_reports`. Timing captured by ``run_step``
under canonical keys (``per_student_reports``, ``class_stats_curve``, …).
"""

from __future__ import annotations

from xscore.marking.merge_reports import (
    step_25_per_student_reports as _impl_25_reports,
    step_26_class_stats_curve as _impl_26_stats,
    step_27_per_student_pdfs as _impl_27_pdfs,
    step_28_class_report as _impl_28_class,
    step_29_review_queue as _impl_29_review,
)
from xscore.shared.path_builders import (
    artifact_student_report_pdf_landscape_path,
    artifact_student_report_pdf_portrait_2up_path,
    artifact_student_report_pdf_portrait_large_path,
    artifact_student_report_pdf_portrait_path,
)
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.terminal_ui import info_line, ok_line, warn_line


def step_25_per_student_reports(ctx: _Ctx) -> None:
    assert ctx.scaffold is not None and ctx.artifact_dir is not None
    _impl_25_reports(ctx)
    n = len(ctx.student_summaries or [])
    ok_line(f"{n} student report" if n == 1 else f"{n} student reports")
    if ctx.failed_students:
        names = ", ".join(s["name"] for s in ctx.failed_students)
        warn_line(f"{len(ctx.failed_students)} student(s) failed to merge: {names}")


def step_26_class_stats(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    _impl_26_stats(ctx)
    summaries = ctx.student_summaries or []
    known = [s["percentage"] for s in summaries if s["percentage"] is not None]
    if len(known) >= 2:
        avg_str = f"{round(sum(known) / len(known), 1)}%"
        ok_line(f"Class avg {avg_str}")
    elif len(known) == 1:
        ok_line("Class avg n/a (single student)")
    else:
        ok_line("Class avg N/A")


def step_27_per_student_pdfs(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    _impl_27_pdfs(ctx)
    n = len(ctx.student_summaries or [])
    s = "" if n == 1 else "s"
    ok_line(f"{n} landscape + {n} portrait + {n} portrait-large + {n} 2UP PDF{s} compiled")
    # Post-check expected outputs: every student should have all 4 PDF variants.
    # Catches both xelatex non-zero exits and "exited 0 but produced no PDF" cases.
    pdf_path_fns = (
        artifact_student_report_pdf_landscape_path,
        artifact_student_report_pdf_portrait_path,
        artifact_student_report_pdf_portrait_large_path,
        artifact_student_report_pdf_portrait_2up_path,
    )
    students_missing: list[str] = []
    for summary in ctx.student_summaries or []:
        name = summary["name"]
        if any(not fn(ctx.artifact_dir, name).is_file() for fn in pdf_path_fns):
            students_missing.append(name)
    if students_missing:
        warn_line(
            f"{len(students_missing)} student(s) missing one or more PDFs: "
            + ", ".join(students_missing)
        )


def step_28_class_report(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    result = _impl_28_class(ctx)
    if result == "done":
        n = len(ctx.student_summaries or [])
        ok_line(f"Class report compiled  ·  {n} student{'s' if n != 1 else ''}")
    elif result == "skipped_empty":
        info_line("Skipped — no student summaries to compile")
    # "skipped_filter": _impl_28_class already printed a warn_line; don't double up


def step_29_review_queue(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    n = _impl_29_review(ctx)
    if n:
        ok_line(f"{n} mark{'s' if n != 1 else ''} flagged for review")
        _print_review_queue_breakdown(ctx)
    else:
        ok_line("No marks flagged for review")


def _qnum_natural_key(qnum: str) -> tuple:
    """Sort '2' before '10', and 'Q_2' suffix after its base."""
    base, _, suffix = qnum.partition("_")
    try:
        return (0, int(base), suffix)
    except ValueError:
        return (1, qnum, "")


def _print_review_queue_breakdown(ctx: _Ctx) -> None:
    """Per-student list of flagged exercise numbers with scan-page references."""
    if not ctx.full_reports:
        return
    student_to_pages: dict[str, list[int]] = {
        a.student_name: list(a.page_numbers) for a in (ctx.page_assignments or [])
    }
    by_student: dict[str, list[tuple[str, int | None]]] = {}
    for student in ctx.full_reports:
        pages = student_to_pages.get(student, [])
        for q in ctx.full_reports[student].get("questions") or []:
            conf = (q.get("confidence") or "").strip().lower()
            if conf in ("", "high"):
                continue
            qnum = str(q.get("number", "?"))
            p_label = q.get("page_label")
            scan_page: int | None = None
            if isinstance(p_label, int) and 1 <= p_label <= len(pages):
                scan_page = pages[p_label - 1]
            by_student.setdefault(student, []).append((qnum, scan_page))

    for student in sorted(by_student):
        items = sorted(by_student[student], key=lambda x: _qnum_natural_key(x[0]))
        fragments = [
            f"Q{qnum} (p.{scan_page})" if scan_page is not None else f"Q{qnum} (p.?)"
            for qnum, scan_page in items
        ]
        info_line(f"{student}: {', '.join(fragments)}")
