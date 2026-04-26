"""Step orchestrators (steps 25–29) for the marking pipeline.

Thin entry points — actual work lives in:
- :mod:`xscore.marking.student_merge`   (per-student page merge, name discovery)
- :mod:`xscore.marking.report_xml`      (XML serialisation + post-hoc loader)
- :mod:`xscore.marking.report_markdown` (Markdown rendering)
- :mod:`xscore.marking.report_latex`    (LaTeX rendering)
- :mod:`xscore.marking.class_report`    (class statistics, grade curve, PDF compile, review queue)
"""

from __future__ import annotations

import json
import os
from typing import Any

from xscore.marking.class_report import (
    _apply_grade_curve, _build_class_report, _curved_grade_visible,
    _effective_curve_target, _pass2_write_tex, _write_review_queue,
)
from xscore.marking.formats import get_marking_format
from xscore.marking.report_xml import (  # re-export for legacy importers
    load_student_results_from_reports,  # noqa: F401
)
from xscore.marking.student_merge import (
    _build_answer_lookup, _derive_student_names, _pass1_merge_students,
)
from xscore.shared.exam_paths import (
    artifact_class_report_dir,
    artifact_class_stats_json_path,
    artifact_marking_students_dir,
    artifact_student_pdfs_dir,
    artifact_student_reports_dir,
)


# ---------------------------------------------------------------------------
# Step 25 — Per-student reports (XML + MD)
# ---------------------------------------------------------------------------

def step_25_per_student_reports(ctx: Any) -> None:
    """Merge per-page marking results into per-student XML + MD reports.

    Populates ``ctx.student_summaries``, ``ctx.full_reports``, ``ctx.q_totals``
    for downstream steps (26–29) to consume.

    Honours ``--student`` CLI filter (lower-case exact match): downstream
    class-report step (28) is skipped when the filter is active because a
    one-or-two-student "class" average would be misleading.
    """
    fmt = get_marking_format()
    total_max_marks = ctx.scaffold.total_marks
    correct_answers, marking_criteria_by_num = _build_answer_lookup(ctx)
    names = _derive_student_names(ctx.artifact_dir, fmt=fmt)
    workers = int(os.environ.get("REPORT_COMPILE_WORKERS", os.environ.get("MARKING_WORKERS", "4")))

    cli_student_filter = getattr(ctx, "student_filter", None)
    if cli_student_filter:
        wanted = set(cli_student_filter)
        names = [n for n in names if n.strip().lower() in wanted]

    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_marking_students_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    artifact_student_reports_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)

    student_summaries, full_reports, q_totals, failed, collisions = _pass1_merge_students(
        ctx, fmt, names, total_max_marks, correct_answers, marking_criteria_by_num, workers
    )
    ctx.student_summaries = student_summaries
    ctx.full_reports = full_reports
    ctx.q_totals = q_totals
    ctx.failed_students = failed
    ctx.mark_collisions = collisions


# ---------------------------------------------------------------------------
# Step 26 — Class statistics + grade curve
# ---------------------------------------------------------------------------

def step_26_class_stats_curve(ctx: Any) -> None:
    """Compute class average + grade-curve offset.

    Target priority: prompt override ``ctx.instruction.curved_grade_override``
    → env var ``GRADE_CURVE_TARGET`` (default 80). Mutates
    ``ctx.student_summaries`` in place to add ``curved_pct``, and writes a
    small ``class_stats.json`` artifact recording the resolved target/offset
    so steps 27 and 28 use identical numbers.
    """
    summaries = ctx.student_summaries
    target = _effective_curve_target(ctx)
    _apply_grade_curve(summaries, target)
    known = [s["percentage"] for s in summaries if s["percentage"] is not None]
    class_avg = int(round(sum(known) / len(known))) if known else None
    curve_offset = (target - class_avg) if class_avg is not None else 0
    p = artifact_class_stats_json_path(ctx.artifact_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({
            "class_average_pct": class_avg,
            "curve_offset": curve_offset,
            "curve_target": target,
            "n_students": len(summaries),
            "n_with_marks": len(known),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Step 27 — Per-student PDFs
# ---------------------------------------------------------------------------

def step_27_per_student_pdfs(ctx: Any) -> None:
    """Write per-student .tex files then compile them in parallel via xelatex.

    Visibility of the curved % in each per-student PDF header follows
    ``ctx.instruction.curved_grade_visible`` first, then env var
    ``CURVED_GRADE_VISIBLE`` (default true).
    """
    exam_name = ctx.artifact_dir.parent.name
    workers = int(os.environ.get("REPORT_COMPILE_WORKERS", os.environ.get("MARKING_WORKERS", "4")))
    show_curved_grade = _curved_grade_visible(ctx)
    artifact_student_pdfs_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    _pass2_write_tex(
        ctx.student_summaries, ctx.full_reports, ctx.artifact_dir, exam_name, workers,
        show_curved_grade=show_curved_grade,
    )


# ---------------------------------------------------------------------------
# Step 28 — Class report
# ---------------------------------------------------------------------------

def step_28_class_report(ctx: Any) -> str:
    """Build & write class XML/MD/TeX/PDF + concat combined PDF.

    Returns a discriminator so the wrapper can pick the right summary line:
    ``"done"`` (work ran), ``"skipped_filter"`` (``--student`` filter active —
    a warning was already emitted), ``"skipped_empty"`` (no per-student
    summaries to compile).
    """
    cli_student_filter = getattr(ctx, "student_filter", None)
    if cli_student_filter:
        from xscore.shared.terminal_ui import warn_line
        warn_line(
            "--student filter active — skipping class report (would not be "
            "representative of the full class)."
        )
        return "skipped_filter"
    if not ctx.student_summaries:
        return "skipped_empty"
    exam_name = ctx.artifact_dir.parent.name
    artifact_class_report_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    _build_class_report(ctx, ctx.student_summaries, ctx.q_totals, exam_name)
    return "done"


# ---------------------------------------------------------------------------
# Step 29 — Review queue
# ---------------------------------------------------------------------------

def step_29_review_queue(ctx: Any) -> int:
    """Emit the side-channel review queue (medium / low confidence marks + collisions).

    Always runs, even when no entries are flagged, so downstream tooling can
    rely on the artifact existing. Returns the count of flagged entries.
    Cross-page mark collisions captured by step 25 are appended to the same
    artifact under ``"collisions"``.
    """
    return _write_review_queue(
        ctx.full_reports, ctx.artifact_dir,
        collisions=getattr(ctx, "mark_collisions", None) or None,
    )


# ---------------------------------------------------------------------------
# Backward-compat shim: still callable by anything that hasn't migrated.
# ---------------------------------------------------------------------------

def compile_reports(ctx: Any) -> list[dict]:
    """Run steps 25–29 in sequence (kept for callers not yet migrated).

    Returns a list of per-student summary dicts (keys: name, total_marks, percentage).
    """
    step_25_per_student_reports(ctx)
    step_26_class_stats_curve(ctx)
    step_27_per_student_pdfs(ctx)
    step_28_class_report(ctx)
    step_29_review_queue(ctx)
    return ctx.student_summaries or []
