"""Scaffold step bodies: layout, cut, parse, cross-page figures, scheme
graphics, assign, parse mark scheme, merge.

Most step bodies share local state (exam_pdf, client, layout_result,
raw_questions, …) so each writes/reads ``ctx.scaffold_state`` rather than
receiving these through individual ``_Ctx`` fields.
``detect_cross_page_context`` is a standalone data transform that does not
touch ``scaffold_state`` — it only reads on-disk artifacts from earlier
steps and ``ctx.empty_exam_has_cover``.

Lifecycle: ``scaffold_setup`` initialises ``ctx.scaffold_state`` (and rehydrates
it on resume); ``scaffold_cleanup`` clears it and removes the temp split PDF
created by ``cut_exam_pdf``. The runner in ``xscore.pipeline.runner`` calls
these around the ``empty_exam`` and ``scaffold_phase_b`` phases.
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
from xscore.scaffold.scheme_graphic_transcribe import (
    transcribe_scheme_graphics_phase,
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
    from eXercise.ai_client import resolve_active_model  # noqa: PLC0415
    from eXercise.qwen_input import model_supports_pdf_input  # noqa: PLC0415
    from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415
    _layout_model, _, _ = resolve_active_model(
        ("DETECT_LAYOUT_MODEL",), default="gemini-2.5-flash",
    )
    if not _layout_model.startswith("gemini") and model_supports_pdf_input(_layout_model):
        announce_ai_input(kind="PDF", note="Qwen, fileid upload")
    else:
        announce_ai_input(kind="JPEG", dpi=72, quality=75)
    state = ctx.scaffold_state
    layout_result, layout_elapsed, layout_model = detect_layout_phase(
        state.client, state.exam_pdf, ctx.artifact_dir,
    )
    state.layout_result = layout_result
    state.layout_elapsed = layout_elapsed
    state.layout_model = layout_model


def cut_exam_pdf(ctx: _Ctx) -> None:
    state = ctx.scaffold_state
    actual_exam_pdf, split_pdf_temp_path, _n_phys, n_split = cut_exam_pdf_phase(
        state.exam_pdf, state.layout_result, ctx.artifact_dir,
        layout_model=state.layout_model, layout_elapsed=state.layout_elapsed,
    )
    state.actual_exam_pdf = actual_exam_pdf
    state.split_pdf_temp_path = split_pdf_temp_path
    state.n_split = n_split


def extract_exam_question_numbers(ctx: _Ctx) -> None:
    """extract_exam_question_numbers: extract question numbers from the empty exam (one cheap call).

    Writes ``17_extract_exam_question_numbers/exam_scaffold.{ext}`` and stores
    the resulting nodes in ``ctx.scaffold_state.scaffold_nodes`` for extract_exam_questions.
    """
    announce_step_model(
        model_env="EXTRACT_EXAM_QUESTION_NUMBERS_MODEL",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    from xscore.scaffold.scaffold_detect import extract_exam_question_numbers
    from xscore.scaffold.ai_scaffold import (
        extract_question_numbers_model_config, _print_detected_summary,
    )
    from xscore.shared.exam_paths import artifact_exam_scaffold_path
    from xscore.shared.subjects import needs_code_formatting
    fmt = state.fmt
    detect_model, detect_thinking, detect_max_tokens = extract_question_numbers_model_config()
    from eXercise.ai_client import describe_pdf_input_mode  # noqa: PLC0415
    from xscore.shared.terminal_ui import announce_ai_input, info_line  # noqa: PLC0415
    _kind, _note = describe_pdf_input_mode(detect_model)
    _fallback_dpi = int(os.environ.get("EXTRACT_EXAM_QUESTION_NUMBERS_DPI", "300"))
    announce_ai_input(
        kind=_kind, note=_note,
        dpi=_fallback_dpi if _kind == "PNG" else None,
    )
    info_line(f"Extract question numbers ({detect_model}) …")
    from xscore.shared.response_cache import reuse_cache_enabled  # noqa: PLC0415
    scaffold_nodes, raw_layout = extract_exam_question_numbers(
        state.client,
        detect_model,
        detect_thinking,
        detect_max_tokens,
        actual_exam_pdf=state.actual_exam_pdf,
        layout_result=state.layout_result,
        split_pdf_path=state.split_pdf_temp_path,
        n_split_pages=state.n_split,
        artifact_dir=ctx.artifact_dir,
        fmt=fmt,
        is_cs=needs_code_formatting(ctx),
        should_cache=reuse_cache_enabled(ctx),
    )
    if state.layout_result is not None:
        raw_layout = {
            "rows": state.layout_result.rows,
            "cols": state.layout_result.cols,
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
    state.scaffold_nodes = scaffold_nodes
    state.raw_layout = raw_layout


def extract_exam_questions(ctx: _Ctx) -> None:
    """extract_exam_questions: extract per-question text + options from the empty exam (per-page parallel).

    Reads ``ctx.scaffold_state.scaffold_nodes`` from extract_exam_question_numbers; writes
    ``18_extract_exam_questions/exam_questions.{ext}`` and stores
    ``raw_questions`` on ``scaffold_state`` for downstream steps.
    """
    announce_step_model(
        model_env="EXTRACT_EXAM_QUESTIONS_MODEL",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    from xscore.scaffold.scaffold_fill import extract_exam_questions
    from xscore.scaffold.ai_scaffold import extract_questions_model_config
    from xscore.scaffold.scaffold_markdown import write_raw_exam_markdown
    from xscore.shared.exam_paths import artifact_exam_questions_path
    from xscore.shared.subjects import needs_code_formatting
    fmt = state.fmt
    fill_model, fill_thinking, fill_max_tokens = extract_questions_model_config()
    from eXercise.ai_client import describe_pdf_input_mode  # noqa: PLC0415
    from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415
    _kind, _note = describe_pdf_input_mode(fill_model)
    _fallback_dpi = int(os.environ.get("EXTRACT_EXAM_QUESTIONS_DPI", "300"))
    announce_ai_input(
        kind=_kind, note=_note,
        dpi=_fallback_dpi if _kind == "PNG" else None,
    )
    from xscore.shared.response_cache import reuse_cache_enabled  # noqa: PLC0415
    raw_questions = extract_exam_questions(
        state.client,
        fill_model,
        fill_thinking,
        fill_max_tokens,
        actual_exam_pdf=state.actual_exam_pdf,
        scaffold_nodes=state.scaffold_nodes,
        artifact_dir=ctx.artifact_dir,
        fmt=fmt,
        is_cs=needs_code_formatting(ctx),
        should_cache=reuse_cache_enabled(ctx),
    )
    if ctx.artifact_dir is not None:
        try:
            p = artifact_exam_questions_path(ctx.artifact_dir, fmt=fmt.artifact_ext())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                fmt.serialize_exam(raw_questions, state.raw_layout),
                encoding="utf-8",
            )
            write_raw_exam_markdown(ctx.artifact_dir, raw_questions)
        except OSError as e:
            warn_line(f"Could not save exam questions artifacts: {e}")
    state.raw_questions = raw_questions


def detect_cross_page_context(ctx: _Ctx) -> None:
    """Detect cross-page context — continuation pages, figure refs, parent stems.

    Pure data transform: reads the v1 marking page register from
    ``build_marking_register_v1``, the empty-exam classifications from step
    14, and ``extract_exam_questions``'s ``exam_questions.{yaml|json|xml}``
    artifact. Runs three augmentation passes:

    1. **continuation** — calls whose ``answer_label`` matches an empty-exam
       page classified as ``blank page`` or ``writing space page`` are
       removed and re-attached to the previous question page as extras.
    2. **figures** — figure mentions on a page other than where the figure
       is drawn.
    3. **parents** — child questions on a page after their parent's stem.

    Writes the v2 register at
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
    from xscore.shared.exam_questions_io import load_exam_questions_artifact
    from xscore.shared.path_builders import (
        artifact_continuation_refs_json_path,
        artifact_cross_page_changes_md_path,
        artifact_cross_page_refs_json_path,
        artifact_empty_exam_classifications_json_path,
        artifact_exam_questions_path,
        artifact_handwriting_json_path,
        artifact_marking_page_register_v2_path,
        artifact_parent_refs_json_path,
    )

    if ctx.artifact_dir is None:
        raise RuntimeError('invariant failed: ctx.artifact_dir is not None')

    register = load_register(ctx.artifact_dir)
    if register is None:
        # build_marking_register_v1's writer was newly added — older runs may lack v1.
        register = build_initial_register(ctx)

    # Match the extension the writer (extract_exam_questions) used —
    # fmt.artifact_ext(), not a hardcoded "yaml" — so cross-page detection
    # isn't silently skipped if the scaffold format ever changes.
    questions_path = artifact_exam_questions_path(
        ctx.artifact_dir, fmt=get_scaffold_format().artifact_ext(),
    )
    if not questions_path.exists():
        warn_line(
            f"Skipped — {questions_path} not found "
            "(scaffold phase did not produce parsed exam)."
        )
        return

    # Empty-exam classifications drive the continuation pass. New artifact
    # location first, then the legacy pre-classify_empty_exam_pages-split location for older runs.
    import json
    classifications_path = artifact_empty_exam_classifications_json_path(ctx.artifact_dir)
    if classifications_path.is_file():
        empty_classifications = json.loads(
            classifications_path.read_text(encoding="utf-8")
        ).get("empty_exam_pages", [])
    else:
        legacy_path = artifact_handwriting_json_path(ctx.artifact_dir)
        if legacy_path.is_file():
            empty_classifications = json.loads(
                legacy_path.read_text(encoding="utf-8")
            ).get("empty_exam_pages", [])
        else:
            empty_classifications = []

    _cppd = os.environ.get("CROSS_PAGE_PARENT_DETECTION", "1").strip().lower()
    detect_parents = _cppd in {"1", "true", "yes", "on"}

    exam_questions = load_exam_questions_artifact(questions_path)
    register, figure_refs, parent_refs, continuation_refs = apply_cross_page_extras(
        register, exam_questions, bool(ctx.empty_exam_has_cover),
        empty_classifications=empty_classifications,
        detect_parents=detect_parents,
    )

    write_register(artifact_marking_page_register_v2_path(ctx.artifact_dir), register)

    refs_path = artifact_cross_page_refs_json_path(ctx.artifact_dir)
    refs_path.parent.mkdir(parents=True, exist_ok=True)
    refs_path.write_text(
        json.dumps(figure_refs, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    parent_refs_path = artifact_parent_refs_json_path(ctx.artifact_dir)
    parent_refs_path.write_text(
        json.dumps(parent_refs, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    continuation_refs_path = artifact_continuation_refs_json_path(ctx.artifact_dir)
    continuation_refs_path.write_text(
        json.dumps(continuation_refs, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Per-source counts. A single call can be augmented by multiple passes;
    # report each pass independently so the totals match the register.
    n_cont_calls = sum(
        1 for s in register["students"] for c in s["calls"]
        if any(src == "continuation" for src in c.get("extra_sources") or [])
    )
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
        f"- Continuation pages attached: {len(continuation_refs)}  ·  calls augmented: {n_cont_calls}",
        f"- Figures detected: {len(figure_refs)}  ·  calls augmented: {n_fig_calls}",
        f"- Parent stems detected: {len(parent_refs)}  ·  calls augmented: {n_parent_calls}",
        "",
    ]
    if continuation_refs:
        md_lines.append("## Detected continuation pages")
        md_lines.append("")
        for ref in continuation_refs:
            md_lines.append(
                f"- **{ref['student_name']}** scan p.{ref['scan_page']} "
                f"({ref['page_type']}, answer label {ref['answer_label']}) "
                f"→ attached to answer p.{ref['attached_to_answer_label']} "
                f"(scan p.{ref['attached_to_scan_page']})"
            )
        md_lines.append("")
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

    # Three separate ok_lines so each detection is visible in the terminal.
    if continuation_refs:
        ok_line(
            f"{len(continuation_refs)} continuation page"
            f"{'s' if len(continuation_refs) != 1 else ''} attached  ·  "
            f"{n_cont_calls} call{'s' if n_cont_calls != 1 else ''} augmented"
        )
    else:
        ok_line("No continuation pages attached")
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
        continuation_refs=continuation_refs,
        register=register,
    )


def detect_mark_scheme_graphics(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="DETECT_SCHEME_GRAPHICS_MODEL",
        default_model="gemini-2.5-flash, off",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415
    announce_ai_input(
        kind="PNG", dpi=int(os.environ.get("MARK_SCHEME_GRAPHICS_DPI", "300")),
    )
    state = ctx.scaffold_state
    t0 = time.perf_counter()
    graphics_by_qnum, graphics_questions = detect_scheme_graphics_phase(
        state.answer_pdf, state.raw_questions, ctx.artifact_dir,
        fmt=state.fmt,
    )
    state.graphics_by_qnum = graphics_by_qnum
    n = sum(len(q.get("graphics") or []) for q in (graphics_questions or []))
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
    if state.answer_pdf is None:
        ok_line("Skipped (no mark scheme PDF)")
        state.questions_per_page = {}
        return
    from eXercise.ai_client import describe_pdf_input_mode, resolve_active_model  # noqa: PLC0415
    from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415
    _assign_model, _, _ = resolve_active_model(
        ("ASSIGN_SCHEME_QUESTIONS_MODEL",), default="gemini-2.5-flash",
    )
    _kind, _note = describe_pdf_input_mode(_assign_model)
    announce_ai_input(
        kind=_kind, note=_note,
        dpi=int(os.environ.get("MARK_SCHEME_GRAPHICS_DPI", "300")) if _kind == "PNG" else None,
    )
    from xscore.shared.response_cache import reuse_cache_enabled  # noqa: PLC0415
    mapping = assign_scheme_questions_phase(
        state.client, state.answer_pdf, state.raw_questions, ctx.artifact_dir,
        should_cache=reuse_cache_enabled(ctx),
    )
    state.questions_per_page = mapping
    n_pages = len(mapping)
    n_qs = sum(len(v) for v in mapping.values())
    if n_pages:
        n_parents = _count_parent_stems(state.raw_questions)
        suffix = (
            f"  ·  {n_parents} parent stem(s) with marks=0 not assigned"
            if n_parents else ""
        )
        ok_line(
            f"{n_qs} question(s) mapped across {n_pages} page(s)"
            f"{suffix}"
            f"  ·  {format_duration(time.perf_counter() - t0)}"
        )


def _count_parent_stems(raw_questions: list[dict]) -> int:
    """Count nodes whose ``marks`` is 0 — parent stems whose children carry
    the marks. The scheme assignment step skips these because they aren't
    answered as a single unit."""
    n = 0
    def visit(node: dict) -> None:
        nonlocal n
        try:
            if int(node.get("marks") or 0) == 0:
                n += 1
        except (TypeError, ValueError):
            pass
        for s in node.get("subquestions") or []:
            visit(s)
    for q in raw_questions or []:
        visit(q)
    return n


def parse_mark_scheme(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="READ_MARK_SCHEME_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    state = ctx.scaffold_state
    t0 = time.perf_counter()
    if state.answer_pdf is not None:
        from eXercise.ai_client import describe_pdf_input_mode, resolve_active_model  # noqa: PLC0415
        from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415
        _scheme_model, _, _ = resolve_active_model(
            ("READ_MARK_SCHEME_MODEL", "AI_DEFAULT_MODEL"),
        )
        _kind, _note = describe_pdf_input_mode(_scheme_model)
        announce_ai_input(
            kind=_kind, note=_note,
            dpi=int(os.environ.get("MARK_SCHEME_GRAPHICS_DPI", "300")) if _kind == "PNG" else None,
        )
    from xscore.shared.subjects import needs_code_formatting
    from xscore.shared.response_cache import reuse_cache_enabled  # noqa: PLC0415
    scheme_data = parse_mark_scheme_phase(
        state.client, state.answer_pdf, state.raw_questions,
        state.graphics_by_qnum, state.questions_per_page,
        ctx.artifact_dir, fmt=state.fmt,
        is_cs=needs_code_formatting(ctx),
        should_cache=reuse_cache_enabled(ctx),
    )
    state.scheme_data = scheme_data
    scheme_qs = scheme_data.get("questions", []) if isinstance(scheme_data, dict) else []
    ok_line(
        f"{len(scheme_qs)} answers in mark scheme"
        f"  ·  {format_duration(time.perf_counter() - t0)}"
    )


def transcribe_scheme_graphics(ctx: _Ctx) -> None:
    announce_step_model(
        model_env="TRANSCRIBE_SCHEME_GRAPHIC_MODEL",
        default_model="qwen3.6-plus, 0, 8192",
    )
    from xscore.shared.exam_paths import artifact_mark_scheme_graphics_dir  # noqa: PLC0415
    from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415
    _gfx_dir = artifact_mark_scheme_graphics_dir(ctx.artifact_dir)
    if _gfx_dir.is_dir() and any(_gfx_dir.glob("*.png")):
        announce_ai_input(kind="PNG", note="pre-rendered, detect_mark_scheme_graphics")
    state = ctx.scaffold_state
    t0 = time.perf_counter()
    scheme_data = state.scheme_data
    if scheme_data is None:
        from xscore.shared.exam_paths import artifact_mark_scheme_path
        import yaml as _yaml
        ms_path = artifact_mark_scheme_path(ctx.artifact_dir)
        if ms_path.exists():
            try:
                scheme_data = _yaml.safe_load(ms_path.read_text(encoding="utf-8"))
            except _yaml.YAMLError:
                scheme_data = None
    from xscore.shared.response_cache import reuse_cache_enabled  # noqa: PLC0415
    new, total = transcribe_scheme_graphics_phase(
        state.raw_questions, scheme_data, ctx.artifact_dir,
        should_cache=reuse_cache_enabled(ctx),
    )
    if total == 0:
        ok_line("No graphics to transcribe")
    elif new == total:
        ok_line(
            f"{total} graphic{'s' if total != 1 else ''} transcribed"
            f"  ·  {format_duration(time.perf_counter() - t0)}"
        )
    else:
        ok_line(
            f"{new}/{total} graphic{'s' if total != 1 else ''} transcribed"
            f" ({total - new} reused)  ·  {format_duration(time.perf_counter() - t0)}"
        )


def create_report(ctx: _Ctx) -> None:
    state = ctx.scaffold_state
    t0 = time.perf_counter()
    questions, layout = merge_scaffold_phase(
        state.raw_questions, state.raw_layout, state.scheme_data,
    )
    ctx.scaffold = finalize_scaffold(
        ctx.folder, state.exam_pdf, questions, layout,
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
    (the empty-exam analysis and scaffold steps must be skipped). Idempotent —
    calling it twice is a no-op once ``ctx.scaffold_state.client`` is set.

    When resuming (``ctx.from_step`` set), only short-circuits if the user is
    resuming into a step that doesn't need ``scaffold_state`` — i.e. anything
    in the ``marking_reports_summary`` phase. For ``--from-step`` values that
    fall within ``scaffold_phase_b`` (e.g. 21 = ``detect_cross_page_context``)
    we still populate state so the later scaffold steps that DO read it
    (``detect_mark_scheme_graphics`` onward) can run. The cutoff is computed
    from the registry so renumbering can't reintroduce the old bug where
    ``--from-step extract_exam_questions`` silently no-op'd the user's target step.
    """
    from eXercise.ai_client import make_gemini_native_client
    from xscore.scaffold.scaffold_phase_state import ScaffoldPhaseState
    from xscore.shared.pipeline_steps import STEPS

    if ctx.folder is None or ctx.artifact_dir is None:
        raise RuntimeError('invariant failed: ctx.folder is not None and ctx.artifact_dir is not None')
    if ctx.from_step is not None:
        first_marking = next(
            (s.number for s in STEPS if s.phase == "marking_reports_summary"),
            None,
        )
        if first_marking is not None and ctx.from_step >= first_marking:
            return False
    if ctx.scaffold_state is not None and ctx.scaffold_state.client is not None:
        return True   # already set up

    try:
        exam_pdf = find_exam_pdf(ctx.folder)
    except FileNotFoundError as exc:
        warn_line(f"No exam PDF found — scaffold skipped ({exc})")
        return False
    answer_pdf = find_answer_pdf(ctx.folder)

    from xscore.shared.response_cache import reuse_cache_enabled  # noqa: PLC0415
    client = make_gemini_native_client(should_cache=reuse_cache_enabled(ctx))
    if client is None:
        warn_line(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) not set — scaffold and "
            "marking-reports phases will be skipped."
        )
        return False

    ctx.scaffold_state = ScaffoldPhaseState(
        exam_pdf=exam_pdf,
        answer_pdf=answer_pdf,
        client=client,
        fmt=get_scaffold_format(),
        phase_t0=time.perf_counter(),
    )
    _rehydrate_scaffold_state_on_resume(ctx)
    return True


def _rehydrate_scaffold_state_on_resume(ctx: _Ctx) -> None:
    """Populate ``scaffold_state`` fields that would normally be set by extract_exam_questions.

    When the user resumes into ``scaffold_phase_b`` (currently only
    ``--from-step`` at or past ``detect_cross_page_context``),
    ``extract_exam_question_numbers``/``extract_exam_questions`` are skipped,
    so their on-disk artifacts must be loaded back into ``scaffold_state`` —
    otherwise ``detect_mark_scheme_graphics`` onwards crashes with
    ``AttributeError: raw_questions``.
    """
    from xscore.shared.pipeline_steps import step_by_name
    if (ctx.from_step is None
            or ctx.from_step <= step_by_name("extract_exam_questions").number
            or ctx.artifact_dir is None
            or ctx.scaffold_state is None):
        return
    from xscore.shared.exam_questions_io import load_exam_questions_artifact
    from xscore.shared.exam_paths import artifact_exam_questions_path
    state = ctx.scaffold_state
    fmt = state.fmt
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
    state.raw_questions = data.get("questions") or []
    state.raw_layout = {
        "rows": int(data.get("rows", 1)),
        "cols": int(data.get("cols", 1)),
    }


def scaffold_cleanup(ctx: _Ctx) -> None:
    """Drop the temp split PDF and clear ``scaffold_state``.

    Safe to call regardless of whether setup succeeded; safe to call multiple
    times. Used in the runner's finally so cleanup happens on ``_EarlyExit``
    or unexpected exception.
    """
    if ctx.scaffold_state is None:
        return
    sp: Path | None = ctx.scaffold_state.split_pdf_temp_path
    if sp is not None:
        try:
            sp.unlink()
        except OSError:
            pass
    ctx.scaffold_state = None
