"""AI-based exam scaffold — six step functions for steps 15–20 of the pipeline.

Each step is independently callable with explicit inputs/outputs and writes its
artifacts to its own numbered folder under ``artifact_dir``:

    step15_detect_layout         → 15_detect_exam_layout/
    step16_cut_exam_pdf          → 16_cut_exam/
    step17_parse_exam_pdf        → 17_parse_exam_pdf/
    step18_detect_scheme_graphics → 18_detect_mark_scheme_graphics/
    step19_parse_mark_scheme     → 19_parse_mark_scheme/
    step20_merge_scaffold        → 20_create_report/  (via build_scaffold cache)

``build_ai_scaffold`` is kept as a thin orchestrator that calls the six step
functions in sequence with the original ``on_*_complete`` callbacks for
backward compatibility (``generate_scaffold.build_scaffold`` and the web
service still use it). xScore.py's pipeline calls the six functions directly.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Callable
from pathlib import Path

from eXercise.ai_client import make_gemini_native_client

from xscore.scaffold.formats import get_scaffold_format
from xscore.scaffold.scaffold_gemini import (
    _do_exam_call,
    detect_scheme_graphics,
    parse_mark_scheme_pages,
)
from xscore.scaffold.scaffold_layout import (
    _detect_layout,
    _save_layout_artifact,
    _split_pdf_by_layout,
)
from xscore.scaffold.scaffold_prompts import (
    _SYSTEM_LAYOUT,
    _USER_LAYOUT,
    _exam_pdf_model_config,
    _layout_detect_model_config,
    _mark_scheme_model_config,
)
from xscore.scaffold.scaffold_xml import (
    _json_to_question,
    _merge_scheme,
    _norm,
)
from xscore.shared.exam_paths import (
    artifact_exam_input_pdf_path,
    artifact_exam_layout_raw_path,
    artifact_exam_questions_path,
    artifact_scaffold_prompt_path,
    artifact_split_exam_pdf_path,
)
from xscore.shared.models import ExamLayout, Question
from xscore.shared.prompt_logger import save_prompt
from xscore.shared.terminal_ui import ok_line, tool_line, warn_line


# ---------------------------------------------------------------------------
# Step 15 — Detect exam layout
# ---------------------------------------------------------------------------

def step15_detect_layout(
    client,
    exam_pdf: Path,
    artifact_dir: "Path | None",
) -> tuple["object", float, str]:
    """Detect the rows×cols multi-up layout of *exam_pdf* via Gemini.

    Returns ``(layout_result, elapsed_s, model_id)``. ``layout_result`` is a
    ``_LayoutDetectSchema`` with ``rows``, ``cols``, ``reading_order``.
    On failure, falls back to 1×1 with a warning.

    Writes ``15_detect_exam_layout/exam_layout_raw.json``,
    ``exam_layout.{xml,md}`` (the latter without cut info; step 16 re-saves
    with actual ``n_physical_pages`` / ``n_split_pages``).
    """
    layout_model, layout_thinking, layout_max_tokens = _layout_detect_model_config()

    if artifact_dir is not None:
        save_prompt(
            artifact_scaffold_prompt_path(artifact_dir, "detect_layout"),
            model=layout_model, system=_SYSTEM_LAYOUT,
            messages=[{"role": "user", "content": _USER_LAYOUT}],
        )

    layout_result, layout_elapsed, layout_raw_text, layout_error = _detect_layout(
        client, exam_pdf, layout_model,
        thinking_tokens=layout_thinking, max_tokens=layout_max_tokens,
    )

    if artifact_dir is not None and layout_raw_text is not None:
        try:
            raw_path = artifact_exam_layout_raw_path(artifact_dir)
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(layout_raw_text, encoding="utf-8")
        except OSError as e:
            warn_line(f"Could not save raw exam layout: {e}")

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

    # Write initial layout artifact (cut numbers placeholder; step 16 re-saves with real values).
    if artifact_dir is not None:
        _save_layout_artifact(artifact_dir, layout_result, layout_model, layout_elapsed, 0, 0)

    return layout_result, layout_elapsed, layout_model


# ---------------------------------------------------------------------------
# Step 16 — Cut exam PDF (split multi-up into single logical pages)
# ---------------------------------------------------------------------------

def step16_cut_exam_pdf(
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
    and ``n_split_pages`` (was zero from step 15).
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
# Step 17 — Parse exam PDF
# ---------------------------------------------------------------------------

def step17_parse_exam_pdf(
    client,
    actual_exam_pdf: Path,
    layout_result,
    n_split_pages: int,
    split_pdf_path: "Path | None",
    artifact_dir: "Path | None",
    *,
    fmt=None,
) -> tuple[list[dict], dict]:
    """Parse the exam PDF into a question hierarchy via Gemini.

    Returns ``(raw_questions, raw_layout)`` as produced by ``_do_exam_call``.
    Writes ``17_parse_exam_pdf/exam_questions.{json,xml}`` and the raw response.
    """
    if fmt is None:
        fmt = get_scaffold_format()
    exam_model, exam_thinking, exam_max_tokens = _exam_pdf_model_config()

    raw_questions, raw_layout = _do_exam_call(
        client,
        exam_model,
        exam_thinking,
        exam_max_tokens,
        actual_exam_pdf=actual_exam_pdf,
        layout_result=layout_result,
        split_pdf_path=split_pdf_path,
        n_split_pages=n_split_pages,
        artifact_dir=artifact_dir,
        fmt=fmt,
    )

    # Use pre-detected layout (ignore raw_layout from extraction response in split mode).
    if layout_result is not None:
        raw_layout = {"rows": layout_result.rows, "cols": layout_result.cols}

    if artifact_dir is not None:
        try:
            from xscore.scaffold.scaffold_markdown import write_raw_exam_markdown
            p = artifact_exam_questions_path(artifact_dir, fmt=fmt.artifact_ext())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(fmt.serialize_exam(raw_questions, raw_layout), encoding="utf-8")
            write_raw_exam_markdown(artifact_dir, raw_questions)
        except OSError as e:
            warn_line(f"Could not save exam questions artifacts: {e}")

    ok_line(f"{len(raw_questions)} top-level questions extracted")
    return raw_questions, raw_layout


# ---------------------------------------------------------------------------
# Step 18 — Detect mark scheme graphics
# ---------------------------------------------------------------------------

def step18_detect_scheme_graphics(
    marking_scheme_pdf: "Path | None",
    raw_questions: list[dict],
    artifact_dir: "Path | None",
    *,
    fmt=None,
) -> tuple[dict, "list[dict] | None"]:
    """Detect graphics in the mark scheme via vision API.

    Builds the scheme scaffold from *raw_questions* (used as a hint for valid
    question numbers) then delegates to ``scaffold_gemini.detect_scheme_graphics``.

    Returns ``(graphics_by_qnum, graphics_questions)``. When *marking_scheme_pdf*
    is None, both are empty (mark scheme step is skipped).
    """
    if fmt is None:
        fmt = get_scaffold_format()

    if marking_scheme_pdf is None:
        ok_line("Skipped (no mark scheme PDF)")
        return {}, None

    scaffold_str = fmt.build_scheme_scaffold(raw_questions)
    return detect_scheme_graphics(
        marking_scheme_pdf, scaffold_str,
        artifact_dir=artifact_dir, fmt=fmt,
    )


# ---------------------------------------------------------------------------
# Step 19 — Parse mark scheme
# ---------------------------------------------------------------------------

def step19_parse_mark_scheme(
    client,
    marking_scheme_pdf: "Path | None",
    raw_questions: list[dict],
    graphics_by_qnum: "dict[str, list] | None",
    artifact_dir: "Path | None",
    *,
    fmt=None,
) -> dict:
    """Parse the mark scheme into ``{questions: [{number, correct_answer, mark_scheme, ...}]}``.

    Reads per-page PDFs from step 18's pages dir; falls back to splitting the
    PDF if step 18 was skipped. Returns ``{"questions": []}`` when
    *marking_scheme_pdf* is None or the call fails.
    """
    if fmt is None:
        fmt = get_scaffold_format()

    if marking_scheme_pdf is None:
        return {"questions": []}

    scheme_model, scheme_thinking, scheme_max_tokens = _mark_scheme_model_config()
    scaffold_str = fmt.build_scheme_scaffold(raw_questions)

    try:
        return parse_mark_scheme_pages(
            client,
            scheme_model,
            scheme_thinking,
            scheme_max_tokens,
            marking_scheme_pdf=marking_scheme_pdf,
            scaffold_str=scaffold_str,
            graphics_by_qnum=graphics_by_qnum,
            artifact_dir=artifact_dir,
            fmt=fmt,
        )
    except Exception as exc:
        import logging as _log
        _log.warning("ai_scaffold: mark-scheme extraction failed — %s", exc)
        warn_line(f"Mark-scheme extraction failed — grading without criteria\n    {exc}")
        return {"questions": []}


# ---------------------------------------------------------------------------
# Step 20 — Merge scaffold
# ---------------------------------------------------------------------------

def step20_merge_scaffold(
    raw_questions: list[dict],
    raw_layout: dict,
    scheme_data: dict,
) -> tuple[list[Question], ExamLayout]:
    """Merge exam questions + scheme into the final ``(questions, layout)`` pair.

    Suffixes duplicate question numbers (``38`` and ``38_2``), aligns the
    mark scheme's ``_alt`` aliases, merges criteria into the question tree,
    builds ``ExamLayout``, validates question dicts, converts to
    ``Question`` objects, and upgrades zero-mark leaves with criteria to
    one mark.

    Returns ``(questions, layout)``. Caller (``build_scaffold``) wraps this
    in an ``ExamScaffold`` and saves the cache.
    """
    if isinstance(scheme_data.get("questions"), list):
        # Suffix duplicate question numbers in exam questions so that
        # two questions both printed as "38" become "38" and "38_2".
        _seen_rq: dict[str, int] = {}
        for _node in raw_questions:
            _qnum = str(_node.get("number", ""))
            _seen_rq[_qnum] = _seen_rq.get(_qnum, 0) + 1
            if _seen_rq[_qnum] > 1:
                _node["number"] = f"{_qnum}_{_seen_rq[_qnum]}"

        # Warn if duplicate-numbered questions share the same subpage (likely a scaffold error).
        import logging as _log
        _base_pos: dict[str, list] = {}
        for _node in raw_questions:
            _base = re.sub(r"_\d+$", "", str(_node.get("number", "")))
            _base_pos.setdefault(_base, []).append(
                (_node.get("subpage_row"), _node.get("subpage_col"), _node.get("number"))
            )
        for _base, _bpos in _base_pos.items():
            if len(_bpos) > 1:
                _coords = [(_r, _c) for _r, _c, _ in _bpos]
                if len(set(_coords)) < len(_coords):
                    _log.warning(
                        "ai_scaffold: Q%s duplicates share the same subpage — "
                        "possible misclassification: %s", _base, _bpos
                    )

        # Apply the same suffix to mark scheme entries so scheme_map keys align.
        _seen_sq: dict[str, int] = {}
        for _sq in scheme_data["questions"]:
            if not isinstance(_sq, dict) or not _sq.get("number"):
                continue
            _snum = _norm(_sq.get("number", ""))
            _seen_sq[_snum] = _seen_sq.get(_snum, 0) + 1
            if _seen_sq[_snum] > 1:
                _sq["number"] = f"{_sq['number']}_{_seen_sq[_snum]}"

        scheme_map: dict[str, dict] = {}
        for _sq in scheme_data["questions"]:
            if not isinstance(_sq, dict) or not _sq.get("number"):
                continue
            _k = _norm(_sq["number"])
            scheme_map[_k] = _sq
            # The mark scheme AI may use "_alt" for a second occurrence of the
            # same question number while the exam dedup logic uses "_2". Add a
            # numeric alias so both conventions resolve to the same entry.
            _alt_m = re.match(r"^(.+?)_alt(\d*)$", _k)
            if _alt_m:
                _base, _n = _alt_m.group(1), _alt_m.group(2)
                _idx = (int(_n) + 1) if _n else 2
                scheme_map[f"{_base}_{_idx}"] = _sq
        _merge_scheme(raw_questions, scheme_map)

    layout = ExamLayout(
        rows=max(1, int(raw_layout.get("rows") or 1)),
        cols=max(1, int(raw_layout.get("cols") or 1)),
    )

    import logging as _logging
    valid_nodes = []
    for node in raw_questions:
        if not isinstance(node, dict) or "number" not in node:
            _logging.warning("ai_scaffold: skipping question node missing 'number' key: %r", node)
            continue
        valid_nodes.append(node)

    questions = [_json_to_question(node, layout) for node in valid_nodes]
    _fix_zero_mark_leaves(questions)
    return questions, layout


# ---------------------------------------------------------------------------
# Backward-compat orchestrator — kept so generate_scaffold.build_scaffold and
# the web service can keep their existing call signature with callbacks.
# xScore.py calls the six step functions directly via _scaffold_steps.
# ---------------------------------------------------------------------------

def build_ai_scaffold(
    exam_pdf: Path,
    marking_scheme_pdf: Path | None,
    *,
    on_layout_complete: "Callable[[], None] | None" = None,
    on_cut_complete: "Callable[[bool], None] | None" = None,
    on_exam_complete: "Callable[[list[dict]], None] | None" = None,
    on_graphics_complete: "Callable[[list | None], None] | None" = None,
    on_scheme_complete: "Callable[[list[dict]], None] | None" = None,
    artifact_dir: Path | None = None,
) -> tuple[list[Question], ExamLayout]:
    """Run scaffold steps 15–20 in order, firing the per-step callbacks.

    Thin orchestrator preserved for backward compatibility with
    ``generate_scaffold.build_scaffold`` and external callers (e.g. the web
    grade service). xScore.py's pipeline calls the six ``step*`` functions
    directly so each step has its own header, timing, and resume semantics.
    """
    fmt = get_scaffold_format()

    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")

    split_pdf_temp_path: Path | None = None
    try:
        # Step 15 — detect layout
        layout_result, layout_elapsed, layout_model = step15_detect_layout(
            client, exam_pdf, artifact_dir,
        )
        if on_layout_complete is not None:
            on_layout_complete()

        # Step 16 — cut PDF
        actual_exam_pdf, split_pdf_temp_path, n_physical_pages, n_split_pages = (
            step16_cut_exam_pdf(
                exam_pdf, layout_result, artifact_dir,
                layout_model=layout_model, layout_elapsed=layout_elapsed,
            )
        )
        n_cells = layout_result.rows * layout_result.cols
        if on_cut_complete is not None:
            on_cut_complete(n_cells == 1)

        # Step 17 — parse exam PDF
        raw_questions, raw_layout = step17_parse_exam_pdf(
            client, actual_exam_pdf, layout_result,
            n_split_pages, split_pdf_temp_path, artifact_dir, fmt=fmt,
        )
        if on_exam_complete is not None:
            on_exam_complete(raw_questions)

        # Step 18 — detect scheme graphics
        graphics_by_qnum, graphics_questions = step18_detect_scheme_graphics(
            marking_scheme_pdf, raw_questions, artifact_dir, fmt=fmt,
        )
        if on_graphics_complete is not None:
            on_graphics_complete(graphics_questions)

        # Step 19 — parse mark scheme
        scheme_data = step19_parse_mark_scheme(
            client, marking_scheme_pdf, raw_questions,
            graphics_by_qnum, artifact_dir, fmt=fmt,
        )
        if on_scheme_complete is not None and isinstance(scheme_data.get("questions"), list):
            on_scheme_complete(scheme_data["questions"])

        # Step 20 — merge scaffold
        return step20_merge_scaffold(raw_questions, raw_layout, scheme_data)
    finally:
        # Delete temp split PDF (always, even if upload or inference failed)
        if split_pdf_temp_path is not None:
            try:
                split_pdf_temp_path.unlink()
            except OSError:
                pass


def _fix_zero_mark_leaves(questions: list) -> None:
    """Upgrade any leaf question with marks=0 but a marking criterion to marks=1.

    Gemini sometimes returns marks=0 for sub-questions whose mark allocation is
    not explicitly bracketed in the PDF. When a marking criterion exists the
    question is worth at least 1 mark.
    """
    import logging as _log
    for q in questions:
        if q.subquestions:
            _fix_zero_mark_leaves(q.subquestions)
        elif q.marks == 0 and q.marking_criteria:
            _log.warning(
                "ai_scaffold: %s has marks=0 but a marking criterion — upgraded to 1", q.number
            )
            q.marks = 1
