"""Loader for the ``exam_questions.yaml`` artifact written by step 18
(``extract_exam_questions``) and read by both scaffold and marking pipelines.

Lives in ``xscore.shared`` so marking can consume it without importing scaffold
internals.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def load_exam_questions_artifact(path: Path) -> dict:
    """Load a serialized ``exam_questions.yaml`` artifact.

    Returns ``{rows, cols, questions}``. Returns ``{}`` if the file does not exist.
    """
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
