"""Steps 24–28: per-student reports, class stats/curve, per-student PDFs,
class report, review queue.

Each step is a thin wrapper around the corresponding ``step_NN_*`` function
in :mod:`xscore.marking.merge_reports`. Timing captured by ``run_step``
under canonical keys (``per_student_reports``, ``class_stats_curve``, …).
"""

from __future__ import annotations

from xscore.marking.merge_reports import (
    step_24_per_student_reports as _impl_24,
    step_25_class_stats_curve as _impl_25,
    step_26_per_student_pdfs as _impl_26,
    step_27_class_report as _impl_27,
    step_28_review_queue as _impl_28,
)
from xscore.shared.path_builders import (
    artifact_student_report_pdf_landscape_path,
    artifact_student_report_pdf_portrait_2up_path,
    artifact_student_report_pdf_portrait_large_path,
    artifact_student_report_pdf_portrait_path,
)
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.terminal_ui import info_line, ok_line, warn_line


def step_24_per_student_reports(ctx: _Ctx) -> None:
    assert ctx.scaffold is not None and ctx.artifact_dir is not None
    _impl_24(ctx)
    n = len(ctx.student_summaries or [])
    ok_line(f"{n} student report" if n == 1 else f"{n} student reports")
    if ctx.failed_students:
        names = ", ".join(s["name"] for s in ctx.failed_students)
        warn_line(f"{len(ctx.failed_students)} student(s) failed to merge: {names}")


def step_25_class_stats(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    _impl_25(ctx)
    summaries = ctx.student_summaries or []
    known = [s["percentage"] for s in summaries if s["percentage"] is not None]
    if len(known) >= 2:
        avg_str = f"{round(sum(known) / len(known), 1)}%"
        ok_line(f"Class avg {avg_str}")
    elif len(known) == 1:
        ok_line("Class avg n/a (single student)")
    else:
        ok_line("Class avg N/A")


def step_26_per_student_pdfs(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    _impl_26(ctx)
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


def step_27_class_report(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    result = _impl_27(ctx)
    if result == "done":
        n = len(ctx.student_summaries or [])
        ok_line(f"Class report compiled  ·  {n} student{'s' if n != 1 else ''}")
    elif result == "skipped_empty":
        info_line("Skipped — no student summaries to compile")
    # "skipped_filter": _impl_27 already printed a warn_line; don't double up


def step_28_review_queue(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    n = _impl_28(ctx)
    if n:
        ok_line(f"{n} mark{'s' if n != 1 else ''} flagged for review")
    else:
        ok_line("No marks flagged for review")
