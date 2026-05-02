"""Exam-side scaffold phases — operate on the exam paper PDF.

Three phase orchestrators called by :func:`build_ai_scaffold` and
``xscore.steps.scaffold``:

- :func:`detect_layout_phase` — Gemini detects the rows×cols multi-up layout.
- :func:`cut_exam_pdf_phase` — split a multi-up PDF into a single-logical-page PDF.
- :func:`parse_exam_pdf_full` — orchestrates extract_exam_question_numbers + extract_exam_questions.

The mark-scheme-side phases live in :mod:`ai_scaffold_scheme`; the orchestrator
plus merge logic stays in :mod:`ai_scaffold`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from xscore.scaffold.formats import get_scaffold_format
from xscore.scaffold.scaffold_detect import extract_exam_question_numbers
from xscore.scaffold.scaffold_fill import extract_exam_questions
from xscore.scaffold.scaffold_layout import (
    _detect_layout,
    _save_layout_artifact,
    _split_pdf_by_layout,
)
from xscore.scaffold.scaffold_prompts import (
    _SYSTEM_LAYOUT,
    _USER_LAYOUT,
    _extract_question_numbers_model_config,
    _extract_questions_model_config,
    _layout_detect_model_config,
)
from xscore.scaffold.scaffold_qtree import _format_qnums_for_line
from xscore.shared.exam_paths import (
    artifact_exam_input_pdf_path,
    artifact_exam_layout_raw_path,
    artifact_exam_questions_path,
    artifact_exam_scaffold_path,
    artifact_scaffold_prompt_path,
    artifact_split_exam_pdf_path,
)
from xscore.shared.prompt_logger import save_prompt, save_response
from xscore.shared.terminal_ui import (
    get_console, info_line, ok_line, tool_line, warn_line,
)


# ---------------------------------------------------------------------------
# Detect exam layout
# ---------------------------------------------------------------------------

def detect_layout_phase(
    client,
    exam_pdf: Path,
    artifact_dir: "Path | None",
) -> tuple["object", float, str]:
    """Detect the rows×cols multi-up layout of *exam_pdf* via Gemini.

    Returns ``(layout_result, elapsed_s, model_id)``. ``layout_result`` is a
    ``_LayoutDetectSchema`` with ``rows``, ``cols``, ``reading_order``.
    On failure, falls back to 1×1 with a warning.

    Writes ``15_detect_exam_layout/exam_layout_raw.json``,
    ``exam_layout.{xml,md}`` (the latter without cut info; this phase re-saves
    with actual ``n_physical_pages`` / ``n_split_pages``).
    """
    layout_model, layout_thinking, layout_max_tokens = _layout_detect_model_config()

    (
        layout_result, layout_elapsed, layout_raw_text, layout_thinking_text,
        layout_error, layout_audit_messages,
    ) = _detect_layout(
        client, exam_pdf, layout_model,
        thinking_tokens=layout_thinking, max_tokens=layout_max_tokens,
    )

    if artifact_dir is not None and layout_audit_messages:
        save_prompt(
            artifact_scaffold_prompt_path(artifact_dir, "detect_layout"),
            model=layout_model, messages=layout_audit_messages,
        )

    if artifact_dir is not None and layout_raw_text is not None:
        try:
            raw_path = artifact_exam_layout_raw_path(artifact_dir)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(layout_raw_text, encoding="utf-8")
        except OSError as e:
            warn_line(f"Could not save raw exam layout: {e}")
        _layout_prompt_path = artifact_scaffold_prompt_path(artifact_dir, "detect_layout")
        save_response(
            _layout_prompt_path,
            layout_raw_text, thinking=layout_thinking_text,
        )
        from xscore.shared.prompt_logger import save_output_data
        save_output_data(_layout_prompt_path, layout_raw_text, ext="json")

    n_cells = layout_result.rows * layout_result.cols
    if layout_error is not None:
        warn_line(
            f"Layout detection failed — assuming 1×1"
            f"  ·  {layout_model}  ·  {layout_elapsed:.1f}s"
            f"\n    {layout_error}"
        )
    elif n_cells > 1:
        ok_line(
            f"Layout {layout_result.rows}×{layout_result.cols} ({n_cells}-up)"
            f"  ·  {layout_model}  ·  {layout_elapsed:.1f}s"
        )
    else:
        ok_line(f"Layout 1×1 (single)  ·  {layout_model}  ·  {layout_elapsed:.1f}s")

    # Write initial layout artifact (cut numbers placeholder; the cut phase re-saves with real values).
    if artifact_dir is not None:
        _save_layout_artifact(artifact_dir, layout_result, layout_model, layout_elapsed, 0, 0)

    return layout_result, layout_elapsed, layout_model


# ---------------------------------------------------------------------------
# Cut exam PDF (split multi-up into single logical pages)
# ---------------------------------------------------------------------------

def cut_exam_pdf_phase(
    exam_pdf: Path,
    layout_result,
    artifact_dir: "Path | None",
    *,
    layout_model: str = "",
    layout_elapsed: float = 0.0,
) -> tuple[Path, "Path | None", int, int]:
    """Split *exam_pdf* by *layout_result* into a single-logical-page PDF.

    Returns ``(actual_exam_pdf, split_pdf_temp_path, n_physical_pages, n_split_pages)``.
    For 1×1 layouts: returns the original *exam_pdf* and ``split_pdf_temp_path=None``.
    For multi-up layouts: writes ``16_cut_exam/split_exam.pdf`` and returns its path.

    Caller is responsible for unlinking *split_pdf_temp_path* (the underlying
    temp file produced by ``_split_pdf_by_layout``) once parsing finishes.
    Also updates the step-15 layout artifact to record ``n_physical_pages``
    and ``n_split_pages`` (was zero before the cut).
    """
    n_cells = layout_result.rows * layout_result.cols
    split_pdf_temp_path: Path | None = None
    n_physical_pages = 0
    n_split_pages = 0

    if n_cells > 1:
        layout_label = f"{layout_result.rows}×{layout_result.cols}"
        tool_line("split", f"Splitting exam PDF ({layout_label} layout) …")
        split_pdf_temp_path, n_physical_pages, n_split_pages = _split_pdf_by_layout(
            exam_pdf, layout_result
        )
        ok_line(f"{n_physical_pages} physical page(s) → {n_split_pages} sub-pages")
        if artifact_dir is not None:
            try:
                dest = artifact_split_exam_pdf_path(artifact_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(split_pdf_temp_path), str(dest))
            except OSError as e:
                warn_line(f"Could not copy split exam PDF to artifacts: {e}")
        actual_exam_pdf = split_pdf_temp_path
    else:
        ok_line("Skipped — 1×1 layout, no splitting needed")
        # In 1×1 mode no split PDF is produced; copy the original so the artifact
        # directory always contains the PDF sent to Gemini.
        if artifact_dir is not None:
            try:
                dest = artifact_exam_input_pdf_path(artifact_dir)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(exam_pdf), str(dest))
            except OSError as e:
                warn_line(f"Could not copy exam PDF to artifacts: {e}")
        actual_exam_pdf = exam_pdf

    # Re-save the step-15 layout artifact with the actual cut counts.
    if artifact_dir is not None:
        _save_layout_artifact(
            artifact_dir, layout_result, layout_model, layout_elapsed,
            n_physical_pages, n_split_pages,
        )

    return actual_exam_pdf, split_pdf_temp_path, n_physical_pages, n_split_pages


# ---------------------------------------------------------------------------
# Parse exam PDF (legacy single-call path)
# ---------------------------------------------------------------------------

def _print_detected_summary(scaffold_nodes: list[dict]) -> None:
    """Render the post-detect listing — per-page qnums + a one-line summary.

    Walks the scaffold tree once collecting:
      - per-page qnums (parents AND leaves; a parent's stem text lives on its
        own page so its number belongs in that page's listing)
      - total marks (sum across every node — parents are typically 0 marks in
        Cambridge schemes, so the sum equals the leaves' total)
      - leaf type counts (multiple_choice / short_answer / calculation /
        long_answer); parents are excluded since they don't get answered as
        a single unit
    """
    per_page: dict[int, list[str]] = {}
    n_top_level = len(scaffold_nodes)
    n_total = 0
    total_marks = 0
    type_counts: dict[str, int] = {}

    def visit(node: dict) -> None:
        nonlocal n_total, total_marks
        n_total += 1
        num = str(node.get("number", "")).strip()
        if num:
            page = max(1, int(node.get("page") or 1))
            per_page.setdefault(page, []).append(num)
        try:
            total_marks += int(node.get("marks") or 0)
        except (TypeError, ValueError):
            pass
        subs = node.get("subquestions") or []
        if not subs:
            qt = str(node.get("question_type", "") or "").strip()
            if qt:
                type_counts[qt] = type_counts.get(qt, 0) + 1
        for s in subs:
            visit(s)

    for q in scaffold_nodes:
        visit(q)

    console = get_console()
    for page in sorted(per_page):
        qnums = per_page[page]
        console.print(f"[dim]     p{page}   {_format_qnums_for_line(qnums)}[/]")

    n_subs = max(0, n_total - n_top_level)
    type_order = ("multiple_choice", "short_answer", "calculation", "long_answer")
    type_labels = {
        "multiple_choice": "MCQ",
        "short_answer":    "short",
        "calculation":     "calc",
        "long_answer":     "long",
    }
    type_summary = ", ".join(
        f"{type_counts[t]} {type_labels[t]}"
        for t in type_order
        if type_counts.get(t)
    )
    parts = [f"{n_top_level} top-level + {n_subs} sub-questions",
             f"{total_marks} marks"]
    if type_summary:
        parts.append(type_summary)
    ok_line("  ·  ".join(parts))


def parse_exam_pdf_full(
    client,
    actual_exam_pdf: Path,
    layout_result,
    n_split_pages: int,
    split_pdf_path: "Path | None",
    artifact_dir: "Path | None",
    *,
    fmt=None,
    is_cs: bool = False,
) -> tuple[list[dict], dict]:
    """Parse the exam PDF into a question hierarchy. Internally split into
    two steps:

    19. ``extract_exam_question_numbers`` — one cheap call returns ``number/type/page/
        subpage/marks`` (no text). Uses ``EXTRACT_EXAM_QUESTION_NUMBERS_MODEL``.
    20. ``extract_exam_questions`` — per-page parallel calls populate ``text`` and
        ``options`` for each question on each page. Uses
        ``EXTRACT_EXAM_QUESTIONS_MODEL``.

    Writes the step 19 artifact ``exam_scaffold.{ext}`` (intermediate, no text)
    and the step 20 artifact ``exam_questions.{ext}`` (final, with text).
    Concrete folder names come from ``xscore/shared/step_folders.py`` so
    renumbering is centralised.

    Returns ``(raw_questions, raw_layout)`` matching the legacy single-call
    contract — same shape every downstream consumer expects.

    *is_cs* gates the CODE_FORMATTING section in the step-20 system prompt
    (step 19 does not extract text and ignores ``is_cs``).
    """
    if fmt is None:
        fmt = get_scaffold_format()

    # --- Step 19 — extract question numbers (cheap; structure only) ---------
    detect_model, detect_thinking, detect_max_tokens = _extract_question_numbers_model_config()
    info_line(f"Extract question numbers ({detect_model}) …")
    scaffold_nodes, raw_layout = extract_exam_question_numbers(
        client,
        detect_model,
        detect_thinking,
        detect_max_tokens,
        actual_exam_pdf=actual_exam_pdf,
        layout_result=layout_result,
        split_pdf_path=split_pdf_path,
        n_split_pages=n_split_pages,
        artifact_dir=artifact_dir,
        fmt=fmt,
        is_cs=is_cs,
    )

    # Use pre-detected layout (ignore raw_layout from the detect response in split mode).
    if layout_result is not None:
        raw_layout = {"rows": layout_result.rows, "cols": layout_result.cols}

    if artifact_dir is not None:
        try:
            p = artifact_exam_scaffold_path(artifact_dir, fmt=fmt.artifact_ext())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                fmt.serialize_scaffold(scaffold_nodes, raw_layout), encoding="utf-8",
            )
        except OSError as e:
            warn_line(f"Could not save exam_scaffold artifact: {e}")

    _print_detected_summary(scaffold_nodes)

    # --- Step 20 — extract per-question text + options (per-page parallel) --
    fill_model, fill_thinking, fill_max_tokens = _extract_questions_model_config()
    raw_questions = extract_exam_questions(
        client,
        fill_model,
        fill_thinking,
        fill_max_tokens,
        actual_exam_pdf=actual_exam_pdf,
        scaffold_nodes=scaffold_nodes,
        artifact_dir=artifact_dir,
        fmt=fmt,
        is_cs=is_cs,
    )

    if artifact_dir is not None:
        try:
            from xscore.scaffold.scaffold_markdown import write_raw_exam_markdown
            p = artifact_exam_questions_path(artifact_dir, fmt=fmt.artifact_ext())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(fmt.serialize_exam(raw_questions, raw_layout), encoding="utf-8")
            write_raw_exam_markdown(artifact_dir, raw_questions)
        except OSError as e:
            warn_line(f"Could not save exam questions artifacts: {e}")

    return raw_questions, raw_layout
