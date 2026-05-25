"""AI marking, per-student/class reports, page-register management.

Public submodules (consumed by ``xscore.steps.marking``, ``xscore.steps.reports``,
``xscore.steps.geometry`` (page_order_check), ``xscore.pipeline.resume``, and ``web/``):

- :mod:`xscore.marking.ai_mark` — main AI-marking step body.
- :mod:`xscore.marking.blueprints` — blueprint builders/parsers.
- :mod:`xscore.marking.extract_answers` — student answer transcription.
- :mod:`xscore.marking.mark_page` — per-page marking helper.
- :mod:`xscore.marking.merge_reports` — fuse marking into per-student reports.
- :mod:`xscore.marking.marking_page_register` — v1/v2 register I/O + cross-page extras.
- :mod:`xscore.marking.parse_instruction` — natural-language CLI prompt parser.
- :mod:`xscore.marking.formats` — ``MarkingFormat``, ``get_marking_format``.
- :mod:`xscore.marking.geometry` — exam geometry detection (also imported by ``xscore.steps.geometry``).
- :mod:`xscore.marking.scheme_graphics_check` — mark-scheme graphic validation.
- :mod:`xscore.marking.blank_page_detection` — heuristic + AI blank-page check.
- :mod:`xscore.marking.page_order_check` — page-order anomaly detection.
- :mod:`xscore.marking.class_report_export` — class-level report data.
- :mod:`xscore.marking.report_latex` / :mod:`xscore.marking.report_latex_text` /
  :mod:`xscore.marking.report_latex_cells` — LaTeX report generation.

Internal submodules (``student_handwriting_check``, ``student_merge``,
``student_names``, ``empty_exam_page_classifier``, ``cross_page_context``,
``review_queue``, etc.) are pipeline-step implementation details.
"""

__all__ = (
    "ai_mark",
    "blueprints",
    "extract_answers",
    "mark_page",
    "merge_reports",
    "marking_page_register",
    "parse_instruction",
    "formats",
    "geometry",
    "scheme_graphics_check",
    "blank_page_detection",
    "page_order_check",
    "class_report_export",
    "report_latex",
    "report_latex_text",
    "report_latex_cells",
)
