"""Factory for the scaffold format.

Call ``get_scaffold_format()`` once per pipeline invocation and pass the
instance to ``detect_exam_scaffold``, ``fill_exam_scaffold``,
``detect_scheme_graphics``, and ``parse_mark_scheme_pages``.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from xscore.scaffold.formats.base import ScaffoldFormat


def get_scaffold_format() -> ScaffoldFormat:
    return ScaffoldFormat()


def load_exam_questions_artifact(path: Path) -> dict:
    """Load a serialized ``exam_questions.yaml`` artifact.

    Returns ``{rows, cols, questions}``. Returns ``{}`` if the file does not exist.
    """
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


__all__ = ["get_scaffold_format", "ScaffoldFormat", "load_exam_questions_artifact"]
