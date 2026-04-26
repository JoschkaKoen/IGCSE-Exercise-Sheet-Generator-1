"""Steps 16–22: scaffold building (layout, cut, parse, scheme graphics, assign,
merge).

Steps 16–22 share local state (exam_pdf, client, layout_result, raw_questions,
…) so each step writes/reads ``ctx.scaffold_state`` rather than receiving these
through individual ``_Ctx`` fields. ``scaffold_phase`` is the orchestrator
that:

1. Looks up the exam/answer PDFs and Gemini client (skipping the whole phase
   when no exam PDF is found).
2. Calls ``run_step`` for each of 16–22 so each gets timing/error capture.
3. In a ``finally``, deletes the temp split PDF created by step 17 and
   consumed by step 18. Always runs, even on ``_EarlyExit``.
"""

from __future__ import annotations

import time
from pathlib import Path

from xscore.scaffold.ai_scaffold import (
    step16_detect_layout,
    step17_cut_exam_pdf,
    step18_parse_exam_pdf,
    step19_detect_scheme_graphics,
    step20_assign_scheme_questions,
    step21_parse_mark_scheme,
    step22_merge_scaffold,
)
from xscore.scaffold.formats import get_scaffold_format
from xscore.scaffold.generate_scaffold import (
    find_answer_pdf,
    find_exam_pdf,
    finalize_scaffold,
)
from xscore.config import GEMINI_MAX_OUTPUT_TOKENS
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.pipeline_steps import run_step, step_by_number
from xscore.shared.terminal_ui import announce_step_model, format_duration, ok_line, warn_line


def step_16_layout(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="DETECT_LAYOUT_MODEL",
        default_model="gemini-2.5-flash, low",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    layout_result, layout_elapsed, layout_model = step16_detect_layout(
        state["client"], state["exam_pdf"], ctx.artifact_dir,
    )
    state["layout_result"] = layout_result
    state["layout_elapsed"] = layout_elapsed
    state["layout_model"] = layout_model


def step_17_cut(ctx: _Ctx) -> None:
    state = ctx.scaffold_state
    actual_exam_pdf, split_pdf_temp_path, _n_phys, n_split = step17_cut_exam_pdf(
        state["exam_pdf"], state["layout_result"], ctx.artifact_dir,
        layout_model=state["layout_model"], layout_elapsed=state["layout_elapsed"],
    )
    state["actual_exam_pdf"] = actual_exam_pdf
    state["split_pdf_temp_path"] = split_pdf_temp_path
    state["n_split"] = n_split


def step_18_parse_exam(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="READ_EXAM_PDF_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    raw_questions, raw_layout = step18_parse_exam_pdf(
        state["client"], state["actual_exam_pdf"], state["layout_result"],
        state["n_split"], state["split_pdf_temp_path"], ctx.artifact_dir,
        fmt=state["fmt"],
    )
    state["raw_questions"] = raw_questions
    state["raw_layout"] = raw_layout


def step_19_scheme_graphics(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="DETECT_SCHEME_GRAPHICS_MODEL",
        default_model="gemini-2.5-flash, off",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    t0 = time.perf_counter()
    graphics_by_qnum, graphics_questions = step19_detect_scheme_graphics(
        state["answer_pdf"], state["raw_questions"], ctx.artifact_dir,
        fmt=state["fmt"],
    )
    state["graphics_by_qnum"] = graphics_by_qnum
    if graphics_questions is None:
        ok_line("Skipped (DETECT_SCHEME_GRAPHICS_MODEL not set)")
    else:
        n = sum(len(q.get("graphics") or []) for q in graphics_questions)
        ok_line(
            f"{n} graphic{'s' if n != 1 else ''} detected"
            f"  ·  {format_duration(time.perf_counter() - t0)}"
        )


def step_20_assign_questions(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="ASSIGN_SCHEME_QUESTIONS_MODEL",
        default_model="gemini-2.5-flash, off",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    t0 = time.perf_counter()
    if state["answer_pdf"] is None:
        ok_line("Skipped (no mark scheme PDF)")
        state["questions_per_page"] = {}
        return
    mapping = step20_assign_scheme_questions(
        state["client"], state["answer_pdf"], state["raw_questions"], ctx.artifact_dir,
    )
    state["questions_per_page"] = mapping
    n_pages = len(mapping)
    n_qs = sum(len(v) for v in mapping.values())
    if n_pages:
        ok_line(
            f"{n_qs} question(s) mapped across {n_pages} page(s)"
            f"  ·  {format_duration(time.perf_counter() - t0)}"
        )


def step_21_parse_scheme(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="READ_MARK_SCHEME_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    t0 = time.perf_counter()
    scheme_data = step21_parse_mark_scheme(
        state["client"], state["answer_pdf"], state["raw_questions"],
        state["graphics_by_qnum"], state.get("questions_per_page"),
        ctx.artifact_dir, fmt=state["fmt"],
        exam_pdf=state["exam_pdf"],
    )
    state["scheme_data"] = scheme_data
    scheme_qs = scheme_data.get("questions", []) if isinstance(scheme_data, dict) else []
    ok_line(
        f"{len(scheme_qs)} answers in mark scheme"
        f"  ·  {format_duration(time.perf_counter() - t0)}"
    )


def step_22_create_report(ctx: _Ctx) -> None:
    state = ctx.scaffold_state
    t0 = time.perf_counter()
    questions, layout = step22_merge_scaffold(
        state["raw_questions"], state["raw_layout"], state["scheme_data"],
    )
    ctx.scaffold = finalize_scaffold(
        ctx.folder, state["exam_pdf"], questions, layout,
        students=ctx.students, artifact_dir=ctx.artifact_dir,
    )
    qs = ctx.scaffold.gradable_questions
    ok_line(
        f"{len(qs)} gradable parts  ·  {ctx.scaffold.total_marks} marks total"
        f"  ·  {format_duration(time.perf_counter() - t0)}"
    )


def scaffold_phase(ctx: _Ctx) -> None:
    """Steps 16–22 with shared-locals + temp-PDF cleanup.

    Skipped entirely when resuming (``ctx.from_step`` set). Aborts cleanly if
    no exam PDF is found. Cleanup runs even on ``_EarlyExit`` from
    ``run_step``.
    """
    from eXercise.ai_client import make_gemini_native_client

    assert ctx.folder is not None and ctx.artifact_dir is not None
    if ctx.from_step:
        return

    try:
        exam_pdf = find_exam_pdf(ctx.folder)
    except FileNotFoundError as exc:
        warn_line(f"No exam PDF found — scaffold skipped ({exc})")
        return
    answer_pdf = find_answer_pdf(ctx.folder)

    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")

    ctx.scaffold_state.update({
        "exam_pdf":   exam_pdf,
        "answer_pdf": answer_pdf,
        "client":     client,
        "fmt":        get_scaffold_format(),
        "phase_t0":   time.perf_counter(),
    })

    try:
        for n in range(16, 23):
            run_step(ctx, step_by_number(n))
    finally:
        sp: Path | None = ctx.scaffold_state.get("split_pdf_temp_path")
        if sp is not None:
            try:
                sp.unlink()
            except OSError:
                pass
        ctx.scaffold_state.clear()
