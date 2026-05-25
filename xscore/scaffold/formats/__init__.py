"""Factory for the scaffold format.

Call ``get_scaffold_format()`` once per pipeline invocation and pass the
instance to ``extract_exam_question_numbers``, ``extract_exam_questions``,
``detect_scheme_graphics``, and ``parse_mark_scheme_pages``.

The exam-questions YAML loader has moved to
:mod:`xscore.shared.exam_questions_io` so the marking pipeline can use it
without importing scaffold internals.
"""

from __future__ import annotations

from xscore.scaffold.formats.base import ScaffoldFormat


def get_scaffold_format() -> ScaffoldFormat:
    return ScaffoldFormat()


__all__ = ["get_scaffold_format", "ScaffoldFormat"]
