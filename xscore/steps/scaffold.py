"""Scaffold step bodies: layout, cut, parse, cross-page figures, scheme
graphics, assign, parse mark scheme, merge.

Most step bodies share local state (exam_pdf, client, layout_result,
raw_questions, …) so each writes/reads ``ctx.scaffold_state`` rather than
receiving these through individual ``_Ctx`` fields.
``detect_cross_page_context`` is a standalone data transform that does not
touch ``scaffold_state`` — it only reads on-disk artifacts from earlier
steps and ``ctx.empty_exam_has_cover``.

``scaffold_phase`` is the orchestrator that:

1. Looks up the exam/answer PDFs and Gemini client (skipping the whole phase
   when no exam PDF is found).
2. Calls ``run_step`` for each scaffold-building step so each gets
   timing/error capture.
3. In a ``finally``, deletes the temp split PDF created by ``cut_exam_pdf``
   and consumed by the parse phase. Always runs, even on ``_EarlyExit``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from xscore.scaffold.ai_scaffold import merge_scaffold_phase
from xscore.scaffold.ai_scaffold_exam import (
    cut_exam_pdf_phase,
    detect_layout_phase,
)
from xscore.scaffold.ai_scaffold_scheme import (
    assign_scheme_questions_phase,
    detect_scheme_graphics_phase,
    parse_mark_scheme_phase,
)
from xscore.scaffold.formats import get_scaffold_format
from xscore.scaffold.generate_scaffold import (
    find_answer_pdf,
    find_exam_pdf,
    finalize_scaffold,
)
from xscore.config import GEMINI_MAX_OUTPUT_TOKENS
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.pipeline_steps import run_step
from xscore.shared.terminal_ui import announce_step_model, format_duration, ok_line, warn_line


def detect_exam_layout(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="DETECT_LAYOUT_MODEL",
        default_model="gemini-2.5-flash, low",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    layout_result, layout_elapsed, layout_model = detect_layout_phase(
        state["client"], state["exam_pdf"], ctx.artifact_dir,
    )
    state["layout_result"] = layout_result
    state["layout_elapsed"] = layout_elapsed
    state["layout_model"] = layout_model


def cut_exam_pdf(ctx: _Ctx) -> None:
    state = ctx.scaffold_state
    actual_exam_pdf, split_pdf_temp_path, _n_phys, n_split = cut_exam_pdf_phase(
        state["exam_pdf"], state["layout_result"], ctx.artifact_dir,
        layout_model=state["layout_model"], layout_elapsed=state["layout_elapsed"],
    )
    state["actual_exam_pdf"] = actual_exam_pdf
    state["split_pdf_temp_path"] = split_pdf_temp_path
    state["n_split"] = n_split


def detect_exam_scaffold(ctx: _Ctx) -> None:
    """Phase A: detect exam scaffold structure (one cheap call).

    Writes ``18_detect_exam_scaffold/exam_scaffold.{ext}`` and stores the
    resulting nodes in ``ctx.scaffold_state['scaffold_nodes']`` for the
    fill phase.
    """
    announce_step_model(
        model_env="DETECT_EXAM_SCAFFOLD_MODEL",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    from xscore.scaffold.scaffold_detect import detect_exam_scaffold
    from xscore.scaffold.ai_scaffold import (
        _detect_scaffold_model_config, _print_detected_summary,
    )
    from xscore.shared.exam_paths import artifact_exam_scaffold_path
    from xscore.shared.subjects import needs_code_formatting
    fmt = state["fmt"]
    detect_model, detect_thinking, detect_max_tokens = _detect_scaffold_model_config()
    from xscore.shared.terminal_ui import info_line
    info_line(f"Detect scaffold ({detect_model}) …")
    scaffold_nodes, raw_layout = detect_exam_scaffold(
        state["client"],
        detect_model,
        detect_thinking,
        detect_max_tokens,
        actual_exam_pdf=state["actual_exam_pdf"],
        layout_result=state["layout_result"],
        split_pdf_path=state["split_pdf_temp_path"],
        n_split_pages=state["n_split"],
        artifact_dir=ctx.artifact_dir,
        fmt=fmt,
        is_cs=needs_code_formatting(ctx),
    )
    if state["layout_result"] is not None:
        raw_layout = {
            "rows": state["layout_result"].rows,
            "cols": state["layout_result"].cols,
        }
    if ctx.artifact_dir is not None:
        try:
            p = artifact_exam_scaffold_path(ctx.artifact_dir, fmt=fmt.artifact_ext())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                fmt.serialize_scaffold(scaffold_nodes, raw_layout), encoding="utf-8",
            )
        except OSError as e:
            warn_line(f"Could not save exam_scaffold artifact: {e}")
    _print_detected_summary(scaffold_nodes)
    state["scaffold_nodes"] = scaffold_nodes
    state["raw_layout"] = raw_layout


def fill_exam_scaffold(ctx: _Ctx) -> None:
    """Phase B: fill scaffold with text + options (per-page parallel).

    Reads ``ctx.scaffold_state['scaffold_nodes']`` from the detect phase;
    writes ``19_fill_exam_scaffold/exam_questions.{ext}`` and stores
    ``raw_questions`` on ``scaffold_state`` for downstream steps.
    """
    announce_step_model(
        model_env="FILL_EXAM_SCAFFOLD_MODEL",
        legacy_model_env="READ_EXAM_PDF_MODEL",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    from xscore.scaffold.scaffold_fill import fill_exam_scaffold
    from xscore.scaffold.ai_scaffold import _fill_scaffold_model_config
    from xscore.scaffold.scaffold_markdown import write_raw_exam_markdown
    from xscore.shared.exam_paths import artifact_exam_questions_path
    from xscore.shared.subjects import needs_code_formatting
    fmt = state["fmt"]
    fill_model, fill_thinking, fill_max_tokens = _fill_scaffold_model_config()
    raw_questions = fill_exam_scaffold(
        state["client"],
        fill_model,
        fill_thinking,
        fill_max_tokens,
        actual_exam_pdf=state["actual_exam_pdf"],
        scaffold_nodes=state["scaffold_nodes"],
        artifact_dir=ctx.artifact_dir,
        fmt=fmt,
        is_cs=needs_code_formatting(ctx),
    )
    if ctx.artifact_dir is not None:
        try:
            p = artifact_exam_questions_path(ctx.artifact_dir, fmt=fmt.artifact_ext())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                fmt.serialize_exam(raw_questions, state["raw_layout"]),
                encoding="utf-8",
            )
            write_raw_exam_markdown(ctx.artifact_dir, raw_questions)
        except OSError as e:
            warn_line(f"Could not save exam questions artifacts: {e}")
    state["raw_questions"] = raw_questions


def detect_cross_page_context(ctx: _Ctx) -> None:
    """Detect cross-page context — figure references AND parent stems.

    Pure data transform: reads the v1 marking page register from
    ``build_marking_register_v1`` plus ``fill_exam_scaffold``'s
    ``exam_questions.{yaml|json|xml}`` artifact, runs two augmentation passes
    (figure mentions on a different page from where the figure is drawn;
    child questions on a different page from a parent's stem/flowchart),
    and writes the v2 register at
    ``<detect_cross_page_context_dir>/marking_page_register.json`` along
    with diagnostic JSON files and a markdown summary. No AI calls.
    """
    from xscore.marking.marking_page_register import (
        apply_cross_page_extras,
        build_initial_register,
        load_register,
        render_cross_page_step_summary,
        write_register,
    )
    from xscore.scaffold.formats import load_exam_questions_artifact
    from xscore.shared.path_builders import (
        artifact_cross_page_changes_md_path,
        artifact_cross_page_refs_json_path,
        artifact_exam_questions_path,
        artifact_marking_page_register_v2_path,
        artifact_parent_refs_json_path,
    )

    assert ctx.artifact_dir is not None

    register = load_register(ctx.artifact_dir)
    if register is None:
        # build_marking_register_v1's writer was newly added — older runs may lack v1.
        register = build_initial_register(ctx)

    questions_path = artifact_exam_questions_path(
        ctx.artifact_dir, fmt="yaml",
    )
    if not questions_path.exists():
        warn_line(
            f"Skipped — {questions_path} not found "
            "(scaffold phase did not produce parsed exam)."
        )
        return

    _cppd = os.environ.get("CROSS_PAGE_PARENT_DETECTION", "1").strip().lower()
    detect_parents = _cppd in {"1", "true", "yes", "on"}

    exam_questions = load_exam_questions_artifact(questions_path)
    register, figure_refs, parent_refs = apply_cross_page_extras(
        register, exam_questions, bool(ctx.empty_exam_has_cover),
        detect_parents=detect_parents,
    )

    write_register(artifact_marking_page_register_v2_path(ctx.artifact_dir), register)

    import json
    refs_path = artifact_cross_page_refs_json_path(ctx.artifact_dir)
    refs_path.parent.mkdir(parents=True, exist_ok=True)
    refs_path.write_text(
        json.dumps(figure_refs, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    parent_refs_path = artifact_parent_refs_json_path(ctx.artifact_dir)
    parent_refs_path.write_text(
        json.dumps(parent_refs, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Per-source counts. A single call can be augmented by both passes; report
    # each pass independently so the totals match what shows up in the register.
    n_fig_calls = sum(
        1 for s in register["students"] for c in s["calls"]
        if any((src or "").startswith("cross_page_fig_") for src in c.get("extra_sources") or [])
    )
    n_parent_calls = sum(
        1 for s in register["students"] for c in s["calls"]
        if any((src or "").startswith("cross_page_parent_") for src in c.get("extra_sources") or [])
    )

    md_lines = [
        "# Cross-page context references",
        "",
        f"- Figures detected: {len(figure_refs)}  ·  calls augmented: {n_fig_calls}",
        f"- Parent stems detected: {len(parent_refs)}  ·  calls augmented: {n_parent_calls}",
        "",
    ]
    if figure_refs:
        md_lines.append("## Detected figure references")
        md_lines.append("")
        for ref in figure_refs:
            referenced = ", ".join(str(p) for p in ref["referenced_on_answer_labels"])
            md_lines.append(
                f"- **Fig. {ref['figure_label']}** — drawn on answer page "
                f"{ref['drawn_on_answer_label']}, referenced on page"
                f"{'s' if len(ref['referenced_on_answer_labels']) != 1 else ''} {referenced}"
            )
        md_lines.append("")
    if parent_refs:
        md_lines.append("## Detected parent-context references")
        md_lines.append("")
        for ref in parent_refs:
            children = ", ".join(ref["child_numbers"])
            child_pages = sorted(set(ref["child_answer_labels"]))
            child_pages_str = ", ".join(str(p) for p in child_pages)
            md_lines.append(
                f"- **Q{ref['parent_number']}** (page {ref['parent_answer_label']}) "
                f"→ {children} (page{'s' if len(child_pages) != 1 else ''} {child_pages_str})"
            )
        md_lines.append("")
    artifact_cross_page_changes_md_path(ctx.artifact_dir).write_text(
        "\n".join(md_lines), encoding="utf-8"
    )

    # Two separate ok_lines so each detection is visible in the terminal.
    if figure_refs:
        ok_line(
            f"{len(figure_refs)} cross-page figure"
            f"{'s' if len(figure_refs) != 1 else ''} detected  ·  "
            f"{n_fig_calls} call{'s' if n_fig_calls != 1 else ''} augmented"
        )
    else:
        ok_line("No cross-page figures detected")
    if not detect_parents:
        ok_line("Cross-page parent-stem detection disabled (CROSS_PAGE_PARENT_DETECTION=false)")
    elif parent_refs:
        ok_line(
            f"{len(parent_refs)} cross-page parent stem"
            f"{'s' if len(parent_refs) != 1 else ''} detected  ·  "
            f"{n_parent_calls} call{'s' if n_parent_calls != 1 else ''} augmented"
        )
    else:
        ok_line("No cross-page parent stems detected")

    render_cross_page_step_summary(
        figure_refs=figure_refs,
        parent_refs=parent_refs,
        register=register,
    )


def detect_mark_scheme_graphics(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="DETECT_SCHEME_GRAPHICS_MODEL",
        default_model="gemini-2.5-flash, off",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    t0 = time.perf_counter()
    graphics_by_qnum, graphics_questions = detect_scheme_graphics_phase(
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


def assign_scheme_questions(ctx: _Ctx) -> None:
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
    mapping = assign_scheme_questions_phase(
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


def parse_mark_scheme(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="READ_MARK_SCHEME_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    t0 = time.perf_counter()
    from xscore.shared.subjects import needs_code_formatting
    scheme_data = parse_mark_scheme_phase(
        state["client"], state["answer_pdf"], state["raw_questions"],
        state["graphics_by_qnum"], state.get("questions_per_page"),
        ctx.artifact_dir, fmt=state["fmt"],
        is_cs=needs_code_formatting(ctx),
    )
    state["scheme_data"] = scheme_data
    scheme_qs = scheme_data.get("questions", []) if isinstance(scheme_data, dict) else []
    ok_line(
        f"{len(scheme_qs)} answers in mark scheme"
        f"  ·  {format_duration(time.perf_counter() - t0)}"
    )


def create_report(ctx: _Ctx) -> None:
    state = ctx.scaffold_state
    t0 = time.perf_counter()
    questions, layout = merge_scaffold_phase(
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


def scaffold_setup(ctx: _Ctx) -> bool:
    """Initialize ``ctx.scaffold_state`` for the scaffold-related steps.

    Returns True on success (state populated), False when no exam PDF is found
    (the empty-exam analysis and scaffold steps must be skipped). Idempotent — calling it twice is a
    no-op once state["client"] is set.

    When resuming (``ctx.from_step`` set), only short-circuits if the user is
    resuming into a step that doesn't need ``scaffold_state`` — i.e. anything
    in the ``marking_reports_summary`` phase. For ``--from-step`` values that
    fall within ``scaffold_phase_b`` (e.g. 21 = ``detect_cross_page_context``)
    we still populate state so the later scaffold steps that DO read it
    (``detect_mark_scheme_graphics`` onward) can run. The cutoff is computed
    from the registry so renumbering can't reintroduce the old bug where
    ``--from-step 20`` silently no-op'd the user's target step.
    """
    from eXercise.ai_client import make_gemini_native_client
    from xscore.shared.pipeline_steps import STEPS

    assert ctx.folder is not None and ctx.artifact_dir is not None
    if ctx.from_step is not None:
        first_marking = next(
            (s.number for s in STEPS if s.phase == "marking_reports_summary"),
            None,
        )
        if first_marking is not None and ctx.from_step >= first_marking:
            return False
    if ctx.scaffold_state.get("client") is not None:
        return True   # already set up

    try:
        exam_pdf = find_exam_pdf(ctx.folder)
    except FileNotFoundError as exc:
        warn_line(f"No exam PDF found — scaffold skipped ({exc})")
        return False
    answer_pdf = find_answer_pdf(ctx.folder)

    client = make_gemini_native_client()
    if client is None:
        warn_line(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) not set — scaffold and "
            "marking-reports phases will be skipped."
        )
        return False

    ctx.scaffold_state.update({
        "exam_pdf":   exam_pdf,
        "answer_pdf": answer_pdf,
        "client":     client,
        "fmt":        get_scaffold_format(),
        "phase_t0":   time.perf_counter(),
    })
    _rehydrate_scaffold_state_on_resume(ctx)
    return True


def _rehydrate_scaffold_state_on_resume(ctx: _Ctx) -> None:
    """Populate ``scaffold_state`` keys that would normally be set by step 20.

    When the user resumes into ``scaffold_phase_b`` (currently only
    ``--from-step 21``), steps 19/20 are skipped, so their on-disk artifacts
    must be loaded back into ``scaffold_state`` — otherwise step 22 onwards
    crashes with ``KeyError: 'raw_questions'``.
    """
    if ctx.from_step is None or ctx.from_step <= 20 or ctx.artifact_dir is None:
        return
    from xscore.scaffold.formats import load_exam_questions_artifact
    from xscore.shared.exam_paths import artifact_exam_questions_path
    state = ctx.scaffold_state
    fmt = state["fmt"]
    questions_path = artifact_exam_questions_path(
        ctx.artifact_dir, fmt=fmt.artifact_ext(),
    )
    data = load_exam_questions_artifact(questions_path)
    if not data:
        warn_line(
            f"Resume: {questions_path} not found — scaffold_phase_b state "
            "will be incomplete and steps 22+ may fail."
        )
        return
    state["raw_questions"] = data.get("questions") or []
    state["raw_layout"] = {
        "rows": int(data.get("rows", 1)),
        "cols": int(data.get("cols", 1)),
    }


def scaffold_cleanup(ctx: _Ctx) -> None:
    """Drop the temp split PDF and clear ``scaffold_state``.

    Safe to call regardless of whether setup succeeded; safe to call multiple
    times. Used in the runner's finally so cleanup happens on ``_EarlyExit``
    or unexpected exception.
    """
    sp: Path | None = ctx.scaffold_state.get("split_pdf_temp_path")
    if sp is not None:
        try:
            sp.unlink()
        except OSError:
            pass
    ctx.scaffold_state.clear()


def scaffold_phase(ctx: _Ctx) -> None:
    """LEGACY — old monolithic scaffold orchestrator.

    Pre-refactor the runner called this once after geometry. The new pipeline
    splits scaffold work into ``scaffold_setup`` + the ``empty_exam`` phase
    (run before geometry) and the ``scaffold_phase_b`` phase (run after).
    Kept as a fallback for callers (e.g. plans, scripts) that still call the
    old name.
    """
    if not scaffold_setup(ctx):
        return
    try:
        from xscore.shared.pipeline_steps import STEPS
        for s in STEPS:
            if s.phase in ("empty_exam", "scaffold_phase_b"):
                run_step(ctx, s)
    finally:
        scaffold_cleanup(ctx)
