"""Step orchestrators for the marking-reports phase of the pipeline.

These are the entry points wired into the registry by ``wire_step_fns`` for
``per_student_reports``, ``class_stats_curve``, ``per_student_pdfs``,
``class_report``, and ``review_queue``. Actual work lives in:

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
    _effective_curve_target, _pass2_write_tex,
)
from xscore.marking.class_report_export import _write_review_queue
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
# Step 26 — Per-student reports (XML + MD)
# ---------------------------------------------------------------------------

def build_per_student_reports(ctx: Any) -> None:
    """Merge per-page marking results into per-student XML + MD reports.

    Populates ``ctx.student_summaries``, ``ctx.full_reports``, ``ctx.q_totals``
    for downstream steps (27–30) to consume.

    Honours ``--student`` CLI filter (lower-case exact match): downstream
    class-report step (29) is skipped when the filter is active because a
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

    (
        student_summaries, full_reports, full_reports_augmented,
        q_totals, failed, collisions,
    ) = _pass1_merge_students(
        ctx, fmt, names, total_max_marks, correct_answers, marking_criteria_by_num, workers
    )
    ctx.student_summaries = student_summaries
    ctx.full_reports = full_reports
    ctx.full_reports_augmented = full_reports_augmented
    ctx.q_totals = q_totals
    ctx.failed_students = failed
    ctx.mark_collisions = collisions

    if full_reports_augmented:
        from xscore.shared.terminal_ui import info_line
        n = len(full_reports_augmented)
        info_line(
            f"{n} student{'s' if n != 1 else ''} have unanswered questions — "
            f"_full reports will be generated"
        )


# ---------------------------------------------------------------------------
# Step 27 — Class statistics + grade curve
# ---------------------------------------------------------------------------

def compute_class_stats(ctx: Any) -> None:
    """Compute class average + grade-curve offset.

    Target priority: prompt override ``ctx.instruction.curved_grade_override``
    → env var ``GRADE_CURVE_TARGET`` (default 80). Mutates
    ``ctx.student_summaries`` in place to add ``curved_pct``, and writes a
    small ``class_stats.json`` artifact recording the resolved target/offset
    so steps 28 and 29 use identical numbers.
    """
    summaries = ctx.student_summaries
    target = _effective_curve_target(ctx)
    _apply_grade_curve(summaries, target)
    known = [s["percentage"] for s in summaries if s["percentage"] is not None]
    class_avg = int(round(sum(known) / len(known))) if known else None
    curve_offset = (target - class_avg) if class_avg is not None else 0
    ctx.class_average_pct = class_avg
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
# Step 28 — Per-student PDFs
# ---------------------------------------------------------------------------

def render_per_student_pdfs(ctx: Any) -> None:
    """Write per-student .tex files then compile them in parallel via xelatex.

    Visibility of the curved % in each per-student PDF header follows
    ``ctx.instruction.curved_grade_visible`` first, then env var
    ``CURVED_GRADE_VISIBLE`` (default true).

    When ``fill_exam_scaffold``'s ``exam_questions.yaml`` exists, additionally
    emits ``exam_questions.pdf`` (one per run) and
    ``*_landscape_with_questions.pdf`` + ``*_portrait_list.pdf`` per student.
    Missing YAML → warn-and-skip; the original four per-student PDFs still
    produce.
    """
    import yaml

    from xscore.marking.report_latex import _build_question_index
    from xscore.shared.path_builders import artifact_exam_questions_path
    from xscore.shared.terminal_ui import warn_line

    exam_name = ctx.artifact_dir.parent.name
    workers = int(os.environ.get("REPORT_COMPILE_WORKERS", os.environ.get("MARKING_WORKERS", "4")))
    show_curved_grade = _curved_grade_visible(ctx)
    artifact_student_pdfs_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)

    questions_path = artifact_exam_questions_path(ctx.artifact_dir, fmt="yaml")
    parsed_questions: list[dict] | None = None
    qmap_by_num: dict[str, dict] = {}
    if questions_path.exists():
        raw = yaml.safe_load(questions_path.read_text(encoding="utf-8")) or {}
        parsed_questions = list(raw.get("questions") or [])
        qmap_by_num = _build_question_index(parsed_questions)
    else:
        warn_line(
            "Skipped exam-questions PDF and with-questions student variants — "
            f"{questions_path} not found."
        )

    class_avg = getattr(ctx, "class_average_pct", None)
    _pass2_write_tex(
        ctx.student_summaries, ctx.full_reports, ctx.artifact_dir, exam_name, workers,
        show_curved_grade=show_curved_grade,
        parsed_questions=parsed_questions,
        qmap_by_num=qmap_by_num,
        class_avg=class_avg,
    )

    # Second pass — companion "_full" PDFs containing rows for unanswered
    # questions on skipped scan pages. Only fires for students whose
    # augmented report differs from the filtered one.
    aug = getattr(ctx, "full_reports_augmented", None) or {}
    if aug:
        aug_summaries = [s for s in ctx.student_summaries if s["name"] in aug]
        _pass2_write_tex(
            aug_summaries, aug, ctx.artifact_dir, exam_name, workers,
            show_curved_grade=show_curved_grade,
            parsed_questions=parsed_questions,
            qmap_by_num=qmap_by_num,
            name_suffix="_full",
            class_avg=class_avg,
        )


# ---------------------------------------------------------------------------
# Step 29 — Class report
# ---------------------------------------------------------------------------

def render_class_report(ctx: Any) -> str:
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
# Step 30 — Review queue
# ---------------------------------------------------------------------------

def build_review_queue(ctx: Any) -> int:
    """Emit the side-channel review queue (medium / low confidence marks + collisions).

    Always runs, even when no entries are flagged, so downstream tooling can
    rely on the artifact existing. Returns the count of flagged entries.
    Cross-page mark collisions captured by step 26 are appended to the same
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
    """Run the report-pipeline tail in sequence (kept for callers not yet migrated).

    Returns a list of per-student summary dicts (keys: name, total_marks, percentage).
    """
    build_per_student_reports(ctx)
    compute_class_stats(ctx)
    render_per_student_pdfs(ctx)
    render_class_report(ctx)
    build_review_queue(ctx)
    return ctx.student_summaries or []
