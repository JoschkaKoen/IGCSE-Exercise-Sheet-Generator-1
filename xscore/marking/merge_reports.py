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
from xscore.marking.student_merge import (
    _build_answer_lookup, _derive_student_names, _pass1_merge_students,
)
from xscore.shared.exam_paths import (
    artifact_class_report_charts_dir,
    artifact_class_report_landscape_dir,
    artifact_class_report_portrait_dir,
    artifact_class_report_summary_dir,
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
    correct_answers, mark_scheme_answer_by_num, explanation_by_num = _build_answer_lookup(ctx)
    names = _derive_student_names(ctx.artifact_dir, fmt=fmt)
    workers = int(os.environ.get("REPORT_COMPILE_WORKERS", os.environ.get("MARKING_WORKERS", "4")))

    cli_student_filter = getattr(ctx, "student_filter", None)
    if cli_student_filter:
        wanted = {n.strip().lower() for n in cli_student_filter}
        names = [n for n in names if n.strip().lower() in wanted]

    cli_limit = getattr(ctx, "limit_students", None)
    if cli_limit:
        names = names[:cli_limit]

    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_marking_students_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    artifact_student_reports_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)

    n_unanswered_out: list[int] = []
    (
        student_summaries, full_reports,
        q_totals, failed, collisions,
    ) = _pass1_merge_students(
        ctx, fmt, names, total_max_marks,
        correct_answers, mark_scheme_answer_by_num, explanation_by_num,
        workers,
        n_unanswered_students_out=n_unanswered_out,
    )
    ctx.student_summaries = student_summaries
    ctx.full_reports = full_reports
    ctx.q_totals = q_totals
    ctx.failed_students = failed
    ctx.mark_collisions = collisions

    n_aug = n_unanswered_out[0] if n_unanswered_out else 0
    if n_aug:
        from xscore.shared.terminal_ui import info_line
        verb = "have" if n_aug != 1 else "has"
        info_line(
            f"{n_aug} student{'s' if n_aug != 1 else ''} {verb} unanswered "
            f"questions on skipped pages — listed inline as (not answered)"
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
    # _apply_grade_curve solves for the offset that lands the post-clip mean
    # on target, returning the actual offset applied (which is generally
    # larger than the naive `target - class_avg` whenever any student would
    # overflow the 100% cap).
    curve_offset = _apply_grade_curve(summaries, target)
    known = [s["percentage"] for s in summaries if s["percentage"] is not None]
    class_avg = int(round(sum(known) / len(known))) if known else None
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

    When ``extract_exam_questions``'s ``exam_questions.{yaml|json|xml}`` exists, additionally
    emits ``exam_questions.pdf`` (one per run) and
    ``*_landscape_with_questions.pdf`` + ``*_portrait_list.pdf`` per student.
    Missing exam-questions artifact → warn-and-skip; the original four
    per-student PDFs still produce.
    """
    from xscore.marking.report_latex import (
        _build_question_index, _scheme_graphics_by_qnum,
    )
    from xscore.scaffold.formats import load_exam_questions_artifact
    from xscore.shared.path_builders import (
        artifact_exam_questions_path, artifact_mark_scheme_graphics_dir,
    )
    from xscore.shared.terminal_ui import info_line, warn_line

    exam_name = ctx.artifact_dir.parent.name
    workers = int(os.environ.get("REPORT_COMPILE_WORKERS", os.environ.get("MARKING_WORKERS", "4")))
    show_curved_grade = _curved_grade_visible(ctx)
    artifact_student_pdfs_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)

    questions_path = artifact_exam_questions_path(
        ctx.artifact_dir, fmt="yaml",
    )
    parsed_questions: list[dict] | None = None
    qmap_by_num: dict[str, dict] = {}
    if questions_path.exists():
        raw = load_exam_questions_artifact(questions_path)
        parsed_questions = list(raw.get("questions") or [])
        qmap_by_num = _build_question_index(parsed_questions)
    else:
        warn_line(
            "Skipped exam-questions PDF and with-questions student variants — "
            f"{questions_path} not found."
        )

    # MCQ-only exams get a tighter table layout in the per-student PDFs:
    # the answer/expected columns shrink to one-letter cells and Reasoning
    # absorbs the freed width. Unset (None) → False, preserving default layout.
    from xscore.marking.blueprints import is_all_mcq_exam
    is_all_mcq = is_all_mcq_exam(parsed_questions or [])

    # Mark-scheme graphics extracted by step 22. The renderer embeds them at
    # the bottom of the Expected column for each affected question; the
    # preamble's \graphicspath points at this directory so .tex needs only
    # the bare filename. Trailing slash matters — without it some xelatex
    # builds resolve `<dir><name>.png` against `<dir>` as a file prefix.
    scheme_graphics_dir_path = artifact_mark_scheme_graphics_dir(ctx.artifact_dir).resolve()
    scheme_graphics_dir = str(scheme_graphics_dir_path) + "/"
    q_to_graphics = _scheme_graphics_by_qnum(ctx.artifact_dir)
    if q_to_graphics:
        n = sum(len(v) for v in q_to_graphics.values())
        info_line(f"Embedding {n} mark-scheme graphic(s) into the Expected column:")
        for qnum, files in sorted(q_to_graphics.items()):
            info_line(f"  Q{qnum} → {', '.join(files)}")

    class_avg = getattr(ctx, "class_average_pct", None)
    _pass2_write_tex(
        ctx.student_summaries, ctx.full_reports, ctx.artifact_dir, exam_name, workers,
        show_curved_grade=show_curved_grade,
        parsed_questions=parsed_questions,
        qmap_by_num=qmap_by_num,
        class_avg=class_avg,
        q_to_graphics=q_to_graphics,
        scheme_graphics_dir=scheme_graphics_dir,
        is_all_mcq=is_all_mcq,
    )


# ---------------------------------------------------------------------------
# Step 29 — Class report
# ---------------------------------------------------------------------------

def render_class_report(ctx: Any) -> str:
    """Build & write class XML/MD/TeX/PDF + concat combined PDF.

    Returns a discriminator so the wrapper can pick the right summary line:
    ``"done"`` (work ran), ``"skipped_filter"`` (``--student`` or
    ``--limit-students`` filter active — a warning was already emitted),
    ``"skipped_empty"`` (no per-student summaries to compile).
    """
    cli_student_filter = getattr(ctx, "student_filter", None)
    cli_limit = getattr(ctx, "limit_students", None)
    if cli_student_filter or cli_limit:
        from xscore.shared.terminal_ui import warn_line
        active = "--student" if cli_student_filter else "--limit-students"
        warn_line(
            f"{active} filter active — skipping class report (would not be "
            "representative of the full class)."
        )
        return "skipped_filter"
    if not ctx.student_summaries:
        return "skipped_empty"
    exam_name = ctx.artifact_dir.parent.name
    artifact_class_report_summary_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    artifact_class_report_charts_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    artifact_class_report_portrait_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    artifact_class_report_landscape_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    _build_class_report(ctx, ctx.student_summaries, ctx.q_totals, exam_name)
    return "done"


# ---------------------------------------------------------------------------
# Step 30 — Review queue
# ---------------------------------------------------------------------------

def build_review_queue(ctx: Any) -> list[dict]:
    """Emit the side-channel confidence audit (every marked question + collisions).

    Always runs, even when no questions were marked, so downstream tooling can
    rely on the artifacts existing. Returns the list of entries (sorted by
    ascending confidence) so the caller can echo the lowest-confidence rows
    to the terminal without rebuilding it. Cross-page mark collisions
    captured earlier in the pipeline are appended to the JSON / Markdown
    artifacts under ``"collisions"``.
    """
    return _write_review_queue(
        ctx.full_reports, ctx.artifact_dir,
        collisions=getattr(ctx, "mark_collisions", None) or None,
        page_assignments=getattr(ctx, "page_assignments", None),
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
