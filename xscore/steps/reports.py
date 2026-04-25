"""Steps 23–27: per-student reports, class stats/curve, per-student PDFs,
class report, review queue.

Each step is a thin wrapper around the corresponding ``step_NN_*`` function
in :mod:`xscore.marking.merge_reports`. Timing captured by ``run_step``
under canonical keys (``per_student_reports``, ``class_stats_curve``, …).
"""

from __future__ import annotations

from xscore.marking.merge_reports import (
    step_23_per_student_reports as _impl_23,
    step_24_class_stats_curve as _impl_24,
    step_25_per_student_pdfs as _impl_25,
    step_26_class_report as _impl_26,
    step_27_review_queue as _impl_27,
)
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.terminal_ui import ok_line


def step_23_per_student_reports(ctx: _Ctx) -> None:
    assert ctx.scaffold is not None and ctx.artifact_dir is not None
    _impl_23(ctx)
    n = len(ctx.student_summaries or [])
    ok_line(f"{n} student report" if n == 1 else f"{n} student reports")


def step_24_class_stats(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    _impl_24(ctx)
    summaries = ctx.student_summaries or []
    known = [s["percentage"] for s in summaries if s["percentage"] is not None]
    if len(known) >= 2:
        avg_str = f"{round(sum(known) / len(known), 1)}%"
        ok_line(f"Class avg {avg_str}")
    elif len(known) == 1:
        ok_line("Class avg n/a (single student)")
    else:
        ok_line("Class avg N/A")


def step_25_per_student_pdfs(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    _impl_25(ctx)
    n = len(ctx.student_summaries or [])
    ok_line(f"{n} landscape + {n} portrait PDF{'s' if n != 1 else ''} compiled")


def step_26_class_report(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    _impl_26(ctx)


def step_27_review_queue(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    _impl_27(ctx)
