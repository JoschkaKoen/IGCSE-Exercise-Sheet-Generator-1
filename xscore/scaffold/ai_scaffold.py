"""AI-based exam scaffold orchestrator.

The seven phase orchestrators are split into two siblings by which input PDF
they process:

- Exam-side phases (:mod:`ai_scaffold_exam`):
  ``detect_layout_phase``, ``cut_exam_pdf_phase``, ``parse_exam_pdf_full``.
- Mark-scheme-side phases (:mod:`ai_scaffold_scheme`):
  ``detect_scheme_graphics_phase``, ``assign_scheme_questions_phase``,
  ``parse_mark_scheme_phase``.

This module owns the seventh phase (:func:`merge_scaffold_phase`) and the
top-level orchestrator (:func:`build_ai_scaffold`) that calls the six phases
in sequence with backward-compat ``on_*_complete`` callbacks. The pipeline
registry calls each step body in :mod:`xscore.steps.scaffold`, which in turn
calls the phase orchestrators directly.

For backward compatibility we re-export ``_print_detected_summary`` and the
``_*_model_config`` helpers so callers in :mod:`xscore.steps.scaffold` that
currently import them from ``ai_scaffold`` keep working without churn.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from eXercise.ai_client import make_gemini_native_client

from xscore.scaffold.ai_scaffold_exam import (
    _print_detected_summary,  # noqa: F401  (re-export for steps/scaffold.py)
    cut_exam_pdf_phase,
    detect_layout_phase,
    parse_exam_pdf_full,
)
from xscore.scaffold.ai_scaffold_scheme import (
    assign_scheme_questions_phase,
    detect_scheme_graphics_phase,
    parse_mark_scheme_phase,
)
from xscore.scaffold.formats import get_scaffold_format
from xscore.scaffold.scaffold_prompts import (  # noqa: F401  (re-exported)
    extract_question_numbers_model_config,
    extract_questions_model_config,
    _layout_detect_model_config,
    _mark_scheme_model_config,
)
from xscore.scaffold.scaffold_xml import (
    _json_to_question,
    _merge_scheme,
    _norm,
)
from xscore.shared.models import ExamLayout, Question


# ---------------------------------------------------------------------------
# Step detect_mark_scheme_graphics — Merge scaffold
# ---------------------------------------------------------------------------

def merge_scaffold_phase(
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
# XScore.py calls the six step functions directly via _scaffold_steps.
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
    is_cs: bool = False,
) -> tuple[list[Question], ExamLayout]:
    """Run the scaffold-building phases in order, firing the per-step callbacks.

    Thin orchestrator preserved for backward compatibility with
    ``generate_scaffold.build_scaffold`` and external callers (e.g. the web
    grade service). XScore.py's pipeline calls the per-step functions
    directly so each step has its own header, timing, and resume semantics.

    *is_cs* gates the ``CODE_FORMATTING`` prompt section in the exam-PDF and
    mark-scheme calls. External callers without subject knowledge default to
    ``False`` (no code formatting). The pipeline path uses
    :func:`xscore.shared.subjects.needs_code_formatting` instead.
    """
    fmt = get_scaffold_format()

    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")

    split_pdf_temp_path: Path | None = None
    try:
        # detect layout
        layout_result, layout_elapsed, layout_model = detect_layout_phase(
            client, exam_pdf, artifact_dir,
        )
        if on_layout_complete is not None:
            on_layout_complete()

        # cut PDF
        actual_exam_pdf, split_pdf_temp_path, n_physical_pages, n_split_pages = (
            cut_exam_pdf_phase(
                exam_pdf, layout_result, artifact_dir,
                layout_model=layout_model, layout_elapsed=layout_elapsed,
            )
        )
        n_cells = layout_result.rows * layout_result.cols
        if on_cut_complete is not None:
            on_cut_complete(n_cells == 1)

        # parse exam PDF (legacy single-call path)
        raw_questions, raw_layout = parse_exam_pdf_full(
            client, actual_exam_pdf, layout_result,
            n_split_pages, split_pdf_temp_path, artifact_dir, fmt=fmt,
            is_cs=is_cs,
        )
        if on_exam_complete is not None:
            on_exam_complete(raw_questions)

        # detect scheme graphics
        graphics_by_qnum, graphics_questions = detect_scheme_graphics_phase(
            marking_scheme_pdf, raw_questions, artifact_dir, fmt=fmt,
        )
        if on_graphics_complete is not None:
            on_graphics_complete(graphics_questions)

        # assign questions to mark scheme pages
        questions_per_page = assign_scheme_questions_phase(
            client, marking_scheme_pdf, raw_questions, artifact_dir,
        )

        # parse mark scheme (filtered per page by the assignment)
        scheme_data = parse_mark_scheme_phase(
            client, marking_scheme_pdf, raw_questions,
            graphics_by_qnum, questions_per_page, artifact_dir, fmt=fmt,
            is_cs=is_cs,
        )
        if on_scheme_complete is not None and isinstance(scheme_data.get("questions"), list):
            on_scheme_complete(scheme_data["questions"])

        # merge scaffold
        return merge_scaffold_phase(raw_questions, raw_layout, scheme_data)
    finally:
        # Delete temp split PDF (always, even if upload or inference failed)
        if split_pdf_temp_path is not None:
            try:
                split_pdf_temp_path.unlink()
            except OSError:
                pass


def _fix_zero_mark_leaves(questions: list) -> None:
    """Bump MCQ leaves with marks=0 to 1; warn and keep 0 for non-MCQ leaves.

    Some Cambridge MCQs have a faintly-printed "[1]" that is OCR'd as 0; rescue
    those silently. Non-MCQ marks=0 is treated as authoritative (e.g. a question
    withdrawn from the paper) and surfaced as a warning so a human can verify.
    """
    from xscore.shared.terminal_ui import warn_line
    for q in questions:
        if q.subquestions:
            _fix_zero_mark_leaves(q.subquestions)
            continue
        if q.marks != 0:
            continue
        if q.question_type == "multiple_choice":
            # Defensive — extract_exam_question_numbers's _parse_yaml_scaffold_node usually already
            # bumps MCQ leaves; this catches paths that bypass that parser.
            q.marks = 1
        else:
            warn_line(
                f"Scaffold: Q{q.number} ({q.question_type}) page {q.page} "
                f"has marks=0 — skipping in marker (verify or fix manually)"
            )
