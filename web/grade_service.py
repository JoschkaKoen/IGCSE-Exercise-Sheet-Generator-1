# -*- coding: utf-8 -*-
"""Shared form-input dataclass for the grade pipeline.

The actual subprocess runner lives in ``web/grade_subprocess.py``; this module
exists to hold the ``GradeFormOpts`` dataclass that both the route handlers and
the subprocess runner consume, and to keep that data shape in one place.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class GradeFormOpts:
    """Form fields for a grade-pipeline submission, mirroring XScore.py CLI flags.

    See ``web/grade_subprocess.build_argv`` for the field → argv mapping.
    ``use_cache`` is web-only: it prepends ``"use cache "`` to the prompt so
    the canonical phrase-heuristic in ``xscore/marking/parse_instruction.py``
    enables the AI marking cache. There is no CLI flag for it.
    """

    prompt: str | None = None
    force_clean_scan: bool = False
    stop_after: int | None = None
    from_step: int | None = None
    resume_dir: Path | None = None
    students: list[str] | None = None
    limit_students: int | None = None
    use_cache: bool = False
