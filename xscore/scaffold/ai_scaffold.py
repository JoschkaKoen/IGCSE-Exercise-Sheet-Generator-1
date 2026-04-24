"""AI-based exam scaffold extraction via Gemini.

Replaces the PyMuPDF heuristic parsing with two Gemini API calls:
  Call 1 — exam PDF  → question hierarchy (text, marks, page, subquestions, MC options)
  Call 2 — mark-scheme PDF (optional) → flat list of correct_answer + marking_criteria

Returns list[Question] with spatial BBox zeroed (page coord preserved) so the
overlay PDF generator produces a clean copy of the exam PDF with no annotations.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from eXercise.ai_client import make_gemini_native_client

from xscore.scaffold.formats import get_scaffold_format
from xscore.scaffold.scaffold_gemini import _do_exam_call, _do_scheme_call, _upload_and_poll
from xscore.scaffold.scaffold_layout import _detect_layout, _save_layout_artifact, _split_pdf_by_layout
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
    artifact_exam_layout_raw_path,
    artifact_exam_questions_path,
    artifact_scaffold_prompt_path,
)
from xscore.shared.models import ExamLayout, Question
from xscore.shared.prompt_logger import save_prompt


# ---------------------------------------------------------------------------
# Public API
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
    """Extract exam structure via Gemini and return a list[Question].

    Args:
        exam_pdf: Path to the exam question-paper PDF.
        marking_scheme_pdf: Optional mark-scheme PDF; skipped when None.
        on_exam_complete: Optional callback invoked with the raw question dicts
            after the first API call (exam extraction) completes successfully.
        on_scheme_complete: Optional callback invoked with the raw scheme question
            dicts after the second API call completes, but *before* the scheme is
            merged into the question tree.  May raise SystemExit(0) to stop before merging.
        artifact_dir: If set, write intermediate JSON + Markdown snapshots under the
            fixed step directories (14, 15, 16, 17). Saves are best-effort; OSError
            is silently ignored.

    Returns:
        Tuple of (list[Question], ExamLayout). Questions have spatial BBox coordinates
        zeroed (page and subpage numbers preserved).

    Raises:
        RuntimeError: GOOGLE_API_KEY unset, file upload failed, or Gemini returns non-JSON.
    """
    fmt = get_scaffold_format()
    exam_model, exam_effort = _exam_pdf_model_config()
    scheme_model, scheme_effort = _mark_scheme_model_config()

    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")

    # State tracked across the try/finally (split PDF must always be cleaned up)
    split_pdf_path: Path | None = None
    n_physical_pages: int = 0
    n_split_pages: int = 0
    layout_result = None
    layout_elapsed: float = 0.0
    layout_model: str = ""

    try:
        from xscore.shared.terminal_ui import ok_line, tool_line, warn_line

        # ---- Steps 15–16: layout detection + PDF cutting ----------------------
        layout_model, layout_effort = _layout_detect_model_config()

        # Save prompt before API call
        if artifact_dir is not None:
            save_prompt(
                artifact_scaffold_prompt_path(artifact_dir, "detect_layout"),
                model=layout_model, system=_SYSTEM_LAYOUT,
                messages=[{"role": "user", "content": _USER_LAYOUT}],
            )

        layout_result, layout_elapsed, layout_raw_text, layout_error = _detect_layout(
            client, exam_pdf, layout_model, layout_effort
        )

        # Save raw AI response immediately (even on failure, if we got a response)
        if artifact_dir is not None and layout_raw_text is not None:
            try:
                raw_path = artifact_exam_layout_raw_path(artifact_dir)
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(layout_raw_text, encoding="utf-8")
            except OSError:
                pass

        # Terminal output
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

        if on_layout_complete is not None:
            on_layout_complete()

        if n_cells > 1:
            layout_label = f"{layout_result.rows}×{layout_result.cols}"
            tool_line("split", f"Splitting exam PDF ({layout_label} layout) …")
            split_pdf_path, n_physical_pages, n_split_pages = _split_pdf_by_layout(
                exam_pdf, layout_result
            )
            ok_line(f"{n_physical_pages} physical page(s) → {n_split_pages} sub-pages")
            if artifact_dir is not None:
                try:
                    import shutil
                    from xscore.shared.exam_paths import artifact_split_exam_pdf_path
                    dest = artifact_split_exam_pdf_path(artifact_dir)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(split_pdf_path), str(dest))
                except OSError:
                    pass
        else:
            ok_line("skipped")

        # Save layout artifact immediately — do not wait for exam call to finish
        if artifact_dir is not None:
            _save_layout_artifact(
                artifact_dir, layout_result, layout_model, layout_elapsed,
                n_physical_pages, n_split_pages,
            )
            # In 1×1 mode no split PDF is produced; copy the original so
            # the artifact directory always contains the PDF sent to Gemini.
            if n_cells == 1:
                try:
                    import shutil
                    from xscore.shared.exam_paths import artifact_exam_input_pdf_path
                    dest = artifact_exam_input_pdf_path(artifact_dir)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(str(exam_pdf), str(dest))
                except OSError:
                    pass

        if on_cut_complete is not None:
            on_cut_complete(n_cells == 1)

        # ---- Step 17: exam extraction ------------------------------------------
        actual_exam_pdf = split_pdf_path if split_pdf_path is not None else exam_pdf
        raw_layout: dict = {}
        raw_questions, raw_layout = _do_exam_call(
            client,
            exam_model,
            exam_effort,
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

        # Layout artifact already saved immediately after detection above.

        # Save step-15 artifacts BEFORE on_exam_complete — the callback may raise
        # SystemExit(0) when --through 15 is used, so anything after it won't run.
        if artifact_dir is not None:
            try:
                from xscore.scaffold.scaffold_markdown import write_raw_exam_markdown
                p = artifact_exam_questions_path(artifact_dir, fmt=fmt.artifact_ext())
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(fmt.serialize_exam(raw_questions, raw_layout), encoding="utf-8")
                write_raw_exam_markdown(artifact_dir, raw_questions)
            except OSError:
                pass

        if on_exam_complete is not None:
            on_exam_complete(raw_questions)

        # ---- Step 10: mark scheme extraction (uses step-9 scaffold) ---------
        if marking_scheme_pdf is not None:
            scaffold_str = fmt.build_scheme_scaffold(raw_questions)
            try:
                scheme_data: dict = _do_scheme_call(
                    client,
                    scheme_model,
                    scheme_effort,
                    marking_scheme_pdf=marking_scheme_pdf,
                    scaffold_str=scaffold_str,
                    artifact_dir=artifact_dir,
                    fmt=fmt,
                    on_graphics_complete=on_graphics_complete,
                )
            except Exception as _exc:
                import logging as _log
                _log.warning("ai_scaffold: mark-scheme extraction failed — %s", _exc)
                warn_line(f"Mark-scheme extraction failed — grading without criteria\n    {_exc}")
                scheme_data = {"questions": []}
        else:
            scheme_data = {"questions": []}

        if isinstance(scheme_data.get("questions"), list):
            # Step-10 XML + markdown already saved inside _do_scheme_call().
            # Notify caller that scheme parse is done, before merging.
            # The callback may raise SystemExit(0) for --through 5.
            if on_scheme_complete is not None:
                on_scheme_complete(scheme_data["questions"])

            # Suffix duplicate question numbers in exam questions so that
            # two questions both printed as "38" become "38" and "38_2".
            # Done after saving artifacts so 10_exam_questions.json retains original numbers.
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
            # Done after saving 11_mark_scheme.json to preserve original numbers there.
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

    finally:
        # Delete temp split PDF (always, even if upload or inference failed)
        if split_pdf_path is not None:
            try:
                split_pdf_path.unlink()
            except OSError:
                pass

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
