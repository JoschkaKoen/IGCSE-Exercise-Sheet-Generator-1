"""Subject identity + behavior flags for the xScore pipeline.

A :class:`Subject` describes pipeline behavior that varies with the exam's
academic discipline — most importantly, whether prompts should include the
``CODE_FORMATTING`` section (true for Computer Science; false for Physics
and others). Detected by detect_subject (``detect_subject``) and exposed to
downstream steps via ``ctx.subject``.

Detection is two-tier:
  1. :func:`detect_subject_from_filenames` — fast filename-substring match
     against ``Subject.filename_patterns`` (e.g. ``"0478"`` → CS). Free, no
     API call.
  2. AI fallback (Gemini native PDF on first 2 pages of the empty exam) —
     only when no filename matched.

Add a new subject by appending one entry to :data:`KNOWN_SUBJECTS` with any
filename patterns you have. To add a new behaviour flag (e.g.
``needs_equation_formatting``), add the field to the dataclass + each
known instance + the consumer that reads it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path
    from xscore.shared.pipeline_ctx import _Ctx


@dataclass(frozen=True)
class Subject:
    name: str
    slug: str
    needs_code_formatting: bool
    filename_patterns: tuple[str, ...] = ()


KNOWN_SUBJECTS: tuple[Subject, ...] = (
    Subject(
        name="IGCSE Computer Science",
        slug="igcse_computer_science",
        needs_code_formatting=True,
        filename_patterns=("0478",),
    ),
    Subject(
        name="IGCSE Physics",
        slug="igcse_physics",
        needs_code_formatting=False,
        filename_patterns=("0625",),
    ),
    Subject(
        name="IGCSE Business Studies",
        slug="igcse_business_studies",
        needs_code_formatting=False,
        filename_patterns=("0450",),
    ),
    Subject(
        name="IGCSE Economics",
        slug="igcse_economics",
        needs_code_formatting=False,
        filename_patterns=("0455",),
    ),
    Subject(
        name="A-Level Physics",
        slug="a_level_physics",
        needs_code_formatting=False,
        filename_patterns=("9702",),
    ),
    Subject(
        name="A-Level Biology",
        slug="a_level_biology",
        needs_code_formatting=False,
        filename_patterns=("9700",),
    ),
    Subject(
        name="A-Level Chemistry",
        slug="a_level_chemistry",
        needs_code_formatting=False,
        filename_patterns=("9701",),
    ),
    Subject(
        name="A-Level Business",
        slug="a_level_business",
        needs_code_formatting=False,
        filename_patterns=("9609",),
    ),
    Subject(
        name="A-Level Economics",
        slug="a_level_economics",
        needs_code_formatting=False,
        filename_patterns=("9708",),
    ),
    Subject(
        name="A-Level Mathematics",
        slug="a_level_mathematics",
        needs_code_formatting=False,
        filename_patterns=("9709",),
    ),
    Subject(
        name="A-Level Further Mathematics",
        slug="a_level_further_mathematics",
        needs_code_formatting=False,
        filename_patterns=("9231",),
    ),
)


def get_subject(name: str) -> Subject:
    """Look up a :class:`Subject` by display name or slug (case-insensitive)."""
    n = name.strip().lower()
    for s in KNOWN_SUBJECTS:
        if s.name.lower() == n or s.slug.lower() == n:
            return s
    raise KeyError(
        f"Unknown subject {name!r}. Known: {[s.name for s in KNOWN_SUBJECTS]}"
    )


def available_subjects_from_env() -> tuple[Subject, ...]:
    """Read ``AVAILABLE_SUBJECTS`` env var → tuple of :class:`Subject`.

    Falls back to ``('IGCSE Computer Science', 'IGCSE Physics')`` when unset.
    Unknown names raise :class:`KeyError` so a typo in ``default.env`` fails
    loudly at startup, not at detection time.
    """
    raw = os.environ.get("AVAILABLE_SUBJECTS", "IGCSE Computer Science,IGCSE Physics")
    names = [s.strip() for s in raw.split(",") if s.strip()]
    return tuple(get_subject(n) for n in names)


def detect_subject_from_filenames(
    pdf_paths: "tuple[Path | None, ...]",
    *,
    candidates: "tuple[Subject, ...] | None" = None,
) -> Subject | None:
    """Return the first :class:`Subject` whose ``filename_patterns`` matches
    any of *pdf_paths*'s names (case-insensitive). ``None`` if no match.

    *candidates* defaults to :func:`available_subjects_from_env` so a subject
    not enabled for this run cannot win the heuristic.
    """
    if candidates is None:
        candidates = available_subjects_from_env()
    for subject in candidates:
        if not subject.filename_patterns:
            continue
        for path in pdf_paths:
            if path is None:
                continue
            name_lower = path.name.lower()
            for pat in subject.filename_patterns:
                if pat.lower() in name_lower:
                    return subject
    return None


def needs_code_formatting(ctx: "_Ctx") -> bool:
    """Whether the exam needs the ``CODE_FORMATTING`` prompt section.

    Single-source-of-truth replacement for the legacy
    ``is_cs_exam(*pdf_paths)`` filename heuristic. Reads ``ctx.subject``
    set by detect_subject (``detect_subject``).
    """
    if ctx.subject is None:
        return False
    return ctx.subject.needs_code_formatting
