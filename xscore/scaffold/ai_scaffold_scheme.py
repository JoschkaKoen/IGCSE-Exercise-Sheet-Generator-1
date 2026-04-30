"""Mark-scheme-side scaffold phases — operate on the marking scheme PDF.

Three phase orchestrators called by :func:`build_ai_scaffold` and
``xscore.steps.scaffold``:

- :func:`detect_scheme_graphics_phase` — vision call to find graphics in the scheme.
- :func:`assign_scheme_questions_phase` — maps which questions appear on each page.
- :func:`parse_mark_scheme_phase` — extracts marking criteria per question.

Each phase returns an empty/safe value when ``marking_scheme_pdf`` is ``None``
(no scheme provided) so the orchestrator can skip cleanly. Failures inside the
underlying calls are logged and downgraded to empty results so the marking
pipeline can still produce per-student PDFs.

The exam-side phases live in :mod:`ai_scaffold_exam`; the orchestrator plus
merge logic stays in :mod:`ai_scaffold`.
"""

from __future__ import annotations

from pathlib import Path

from xscore.scaffold.formats import get_scaffold_format
from xscore.scaffold.scaffold_graphics import detect_scheme_graphics
from xscore.scaffold.scaffold_pages import assign_questions_to_pages
from xscore.scaffold.scaffold_prompts import _mark_scheme_model_config
from xscore.scaffold.scaffold_scheme import parse_mark_scheme_pages
from xscore.shared.terminal_ui import ok_line, warn_line


def detect_scheme_graphics_phase(
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


def assign_scheme_questions_phase(
    client,
    marking_scheme_pdf: "Path | None",
    raw_questions: list[dict],
    artifact_dir: "Path | None",
) -> dict[int, list[str]]:
    """Identify which question numbers' criteria appear on each mark scheme page.

    Returns ``{page_num: [qnum, ...]}``. Empty dict when *marking_scheme_pdf*
    is None, the model env var is unset, or the call fails — parse_mark_scheme then
    falls back to its full-scaffold behavior.
    """
    if marking_scheme_pdf is None:
        return {}
    try:
        return assign_questions_to_pages(
            client, marking_scheme_pdf, raw_questions, artifact_dir,
        )
    except Exception as exc:
        import logging as _log
        _log.warning("ai_scaffold: question-assignment failed — %s", exc)
        warn_line(f"Question-assignment failed — falling back to full scaffold\n    {exc}")
        return {}


def parse_mark_scheme_phase(
    client,
    marking_scheme_pdf: "Path | None",
    raw_questions: list[dict],
    graphics_by_qnum: "dict[str, list] | None",
    questions_per_page: "dict[int, list[str]] | None",
    artifact_dir: "Path | None",
    *,
    fmt=None,
    is_cs: bool = False,
) -> dict:
    """Parse the mark scheme into ``{questions: [{number, correct_answer, mark_scheme, ...}]}``.

    Reads per-page PDFs from step 22's pages dir; falls back to splitting the
    PDF if step 22 was skipped. Uses *questions_per_page* (from step 23) to
    send only the relevant question entries to the AI per page; falls back
    to the full scaffold for any page missing from the mapping.

    *is_cs* gates the ``CODE_FORMATTING`` prompt section. The pipeline caller
    derives this from ``ctx.subject`` (set by detect_subject); legacy / external
    callers (``build_ai_scaffold``, web grade service) default to ``False``.

    Returns ``{"questions": []}`` when *marking_scheme_pdf* is None or the
    call fails.
    """
    if fmt is None:
        fmt = get_scaffold_format()

    if marking_scheme_pdf is None:
        return {"questions": []}

    scheme_model, scheme_thinking, scheme_max_tokens = _mark_scheme_model_config()

    try:
        return parse_mark_scheme_pages(
            client,
            scheme_model,
            scheme_thinking,
            scheme_max_tokens,
            marking_scheme_pdf=marking_scheme_pdf,
            raw_questions=raw_questions,
            questions_per_page=questions_per_page,
            graphics_by_qnum=graphics_by_qnum,
            artifact_dir=artifact_dir,
            fmt=fmt,
            is_cs=is_cs,
        )
    except Exception as exc:
        import logging as _log
        _log.warning("ai_scaffold: mark-scheme extraction failed — %s", exc)
        warn_line(f"Mark-scheme extraction failed — grading without criteria\n    {exc}")
        return {"questions": []}
