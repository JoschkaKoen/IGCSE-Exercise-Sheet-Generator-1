"""Vector exam parsing, scaffold cache, and geometry onto scans.

Public submodules (consumed by ``xscore.steps.scaffold``, ``xscore.steps.geometry``,
``xscore.pipeline.resume``, ``eXam.xscore_adapter``, and ``scripts/``):

- :mod:`xscore.scaffold.ai_scaffold` — ``merge_scaffold_phase``,
  ``extract_question_numbers_model_config`` re-export, ``extract_questions_model_config`` re-export.
- :mod:`xscore.scaffold.ai_scaffold_exam` — ``detect_layout_phase``, ``cut_exam_pdf_phase``.
- :mod:`xscore.scaffold.ai_scaffold_scheme` — ``detect_scheme_graphics_phase``,
  ``assign_scheme_questions_phase``, ``parse_mark_scheme_phase``.
- :mod:`xscore.scaffold.scheme_graphic_transcribe` — ``transcribe_scheme_graphics_phase``.
- :mod:`xscore.scaffold.scaffold_detect` — ``extract_exam_question_numbers``.
- :mod:`xscore.scaffold.scaffold_fill` — ``extract_exam_questions``.
- :mod:`xscore.scaffold.scaffold_prompts` — ``extract_question_numbers_model_config``,
  ``extract_questions_model_config``.
- :mod:`xscore.scaffold.scaffold_markdown` — ``write_raw_exam_markdown``.
- :mod:`xscore.scaffold.generate_scaffold` — ``find_exam_pdf``, ``find_answer_pdf``,
  ``build_scaffold``, ``finalize_scaffold``.
- :mod:`xscore.scaffold.formats` — ``ScaffoldFormat``, ``get_scaffold_format``.
- :mod:`xscore.scaffold.pdf_parser` — vector-parse helpers (consumed by calibration scripts).
- :mod:`xscore.scaffold.draw_boxes_on_empty_exam` — ``write_scaffold_boxes_pdf`` (calibration scripts).

Internal submodules (``scaffold_api``, ``scaffold_qtree``, ``scaffold_pages``,
``scaffold_scheme``, ``scaffold_scheme_pdf``, ``scaffold_graphics``,
``scaffold_xml``, ``scaffold_cache*``, ``scaffold_layout``,
``scheme_graphics_extract``) should not be imported from outside ``xscore/scaffold/``.
"""

__all__ = (
    "ai_scaffold",
    "ai_scaffold_exam",
    "ai_scaffold_scheme",
    "scheme_graphic_transcribe",
    "scaffold_detect",
    "scaffold_fill",
    "scaffold_prompts",
    "scaffold_markdown",
    "generate_scaffold",
    "formats",
    "pdf_parser",
    "draw_boxes_on_empty_exam",
)
