"""Lazy xscore-scaffold adapter for the eXam pre-indexer.

Owns all ``xscore.*`` imports so eXam's coupling to xscore lives in one named
file. Imports are deferred to call time because importing ``xscore.scaffold``
transitively pulls ``google.genai``, ``fitz``, etc. — heavy deps eXam does
not want to pay at module-load time.

Use::

    from eXam.xscore_adapter import load_scaffold_api
    api = load_scaffold_api()
    layout_result, elapsed, model = api.detect_layout_phase(client, paper_path, out_dir)
    ...

Subsequent ``load_scaffold_api()`` calls are cheap — Python caches the imported
modules.
"""

from __future__ import annotations

from types import SimpleNamespace


def load_scaffold_api() -> SimpleNamespace:
    """Import the scaffold functions eXam's pre-indexer needs.

    Returns a ``SimpleNamespace`` with the ten symbols listed below. Each is a
    direct re-export of the corresponding ``xscore.scaffold.*`` function — no
    wrapping, no parameter translation. The namespace is just a colocation
    mechanism so eXam's xscore coupling has one entry point.

    Exported symbols:

    - ``detect_layout_phase`` — :mod:`xscore.scaffold.ai_scaffold_exam`
    - ``cut_exam_pdf_phase`` — :mod:`xscore.scaffold.ai_scaffold_exam`
    - ``assign_scheme_questions_phase`` — :mod:`xscore.scaffold.ai_scaffold_scheme`
    - ``detect_scheme_graphics_phase`` — :mod:`xscore.scaffold.ai_scaffold_scheme`
    - ``parse_mark_scheme_phase`` — :mod:`xscore.scaffold.ai_scaffold_scheme`
    - ``get_scaffold_format`` — :mod:`xscore.scaffold.formats`
    - ``extract_exam_question_numbers`` — :mod:`xscore.scaffold.scaffold_detect`
    - ``extract_exam_questions`` — :mod:`xscore.scaffold.scaffold_fill`
    - ``extract_question_numbers_model_config`` — :mod:`xscore.scaffold.scaffold_prompts`
    - ``extract_questions_model_config`` — :mod:`xscore.scaffold.scaffold_prompts`
    """
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
    from xscore.scaffold.scaffold_detect import extract_exam_question_numbers
    from xscore.scaffold.scaffold_fill import extract_exam_questions
    from xscore.scaffold.scaffold_prompts import (
        extract_question_numbers_model_config,
        extract_questions_model_config,
    )
    return SimpleNamespace(
        detect_layout_phase=detect_layout_phase,
        cut_exam_pdf_phase=cut_exam_pdf_phase,
        assign_scheme_questions_phase=assign_scheme_questions_phase,
        detect_scheme_graphics_phase=detect_scheme_graphics_phase,
        parse_mark_scheme_phase=parse_mark_scheme_phase,
        get_scaffold_format=get_scaffold_format,
        extract_exam_question_numbers=extract_exam_question_numbers,
        extract_exam_questions=extract_exam_questions,
        extract_question_numbers_model_config=extract_question_numbers_model_config,
        extract_questions_model_config=extract_questions_model_config,
    )
