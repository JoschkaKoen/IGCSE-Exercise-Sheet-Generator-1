"""Shared types, paths, CLI helpers, roster and ground-truth I/O.

Public submodules (consumed across xscore, by ``web/`` for the grading UI, and
by ``eXam.xscore_adapter`` indirectly):

- :mod:`xscore.shared.pipeline_steps` — ``STEPS``, ``Step``, ``run_step``,
  ``wire_step_fns``, ``step_by_number``, ``step_by_name``.
- :mod:`xscore.shared.pipeline_ctx` — ``_Ctx``, ``_EarlyExit``.
- :mod:`xscore.shared.step_folders` — folder-name constants for per-step artifacts.
- :mod:`xscore.shared.exam_paths` / :mod:`xscore.shared.path_builders` — artifact path helpers.
- :mod:`xscore.shared.qnum_utils` — ``norm_qnum`` (canonical qnum normalisation,
  shared between scaffold and marking).
- :mod:`xscore.shared.exam_questions_io` — ``load_exam_questions_artifact``
  (read ``exam_questions.yaml`` from disk; shared between scaffold and marking).
- :mod:`xscore.shared.models` — ``ExamScaffold``, ``Question``, ``WritingArea``, etc.
- :mod:`xscore.shared.subjects` — ``Subject``, ``get_subject``, ``needs_code_formatting``.
- :mod:`xscore.shared.terminal_ui` — ``pipeline_step``, ``info_line``, ``ok_line``,
  ``warn_line``, ``announce_*`` helpers (also used by ``web/`` for ANSI rendering).
- :mod:`xscore.shared.prompt_logger` — ``save_prompt``, ``save_response``,
  ``save_output_data`` (mirrors :mod:`eXercise.prompt_logger`; both kept in sync).
- :mod:`xscore.shared.response_parsing` — ``strip_code_fences`` and similar.
- :mod:`xscore.shared.response_cache` — opt-in marking-call cache (gated by prompt).
- :mod:`xscore.shared.run_log` — per-run step-event audit log.
- :mod:`xscore.shared.load_student_list` — roster file I/O.
- :mod:`xscore.shared.find_exam_folder` — natural-language exam-folder resolution.
- :mod:`xscore.shared.student_artifacts` — per-student artifact path helpers.
- :mod:`xscore.shared.cost_report` / :mod:`xscore.shared.timing_report` — run summaries.

Internal submodules: anything not listed above (e.g., format-specific helpers).
"""

__all__ = (
    "pipeline_steps",
    "pipeline_ctx",
    "step_folders",
    "exam_paths",
    "path_builders",
    "qnum_utils",
    "exam_questions_io",
    "models",
    "subjects",
    "terminal_ui",
    "prompt_logger",
    "response_parsing",
    "response_cache",
    "run_log",
    "load_student_list",
    "find_exam_folder",
    "student_artifacts",
    "cost_report",
    "timing_report",
)
