"""Typed transient state shared across the scaffold-building steps.

Replaces the historical ``_Ctx.scaffold_state: dict[str, Any]`` with a real
dataclass so a typo (``state.laytout_result``) fails with ``AttributeError``
at first read instead of silently passing init and dying inside a step body.

Lifecycle (managed by :mod:`xscore.steps.scaffold`):

1. ``scaffold_setup(ctx)`` constructs a ``ScaffoldPhaseState`` from the exam
   PDF, mark scheme PDF, Gemini client, and format instance, and assigns it
   to ``ctx.scaffold_state``. On resume (``--from-step ≥ detect_cross_page_context``),
   ``_rehydrate_scaffold_state_on_resume`` repopulates ``raw_questions`` and
   ``raw_layout`` from the on-disk artifact.
2. Step bodies (``detect_exam_layout`` through ``create_report``) read and
   write fields directly: ``state = ctx.scaffold_state; state.layout_result = ...``.
3. ``scaffold_cleanup(ctx)`` removes the temp split PDF (if any) and sets
   ``ctx.scaffold_state = None``. Always runs in the runner's finally.

Field defaults are all ``None`` (or empty collections) so that the partial-init
resume-rehydrate path works without ``TypeError``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ScaffoldPhaseState:
    """Transient store for shared locals across the scaffold-building steps.

    Set by ``scaffold_setup``; cleared by ``scaffold_cleanup``. Each step body
    reads and writes specific fields — see the per-field comments for which
    step is the producer.
    """

    # Set by scaffold_setup from the project layout.
    exam_pdf: Path | None = None              # input: empty exam PDF
    answer_pdf: Path | None = None            # input: mark scheme PDF (may be None)
    client: Any | None = None                 # native Gemini client (or wrapped tracking variant)
    fmt: Any | None = None                    # ScaffoldFormat instance (Any to avoid import cycle)
    phase_t0: float | None = None             # wall-clock t0 for end-of-phase timing

    # Set by detect_exam_layout (step 6).
    layout_result: Any | None = None          # ai_scaffold_exam layout-detection result
    layout_elapsed: float | None = None
    layout_model: str | None = None

    # Set by cut_exam_pdf (step 7).
    actual_exam_pdf: Path | None = None       # post-cut PDF (may equal exam_pdf if no cut needed)
    split_pdf_temp_path: Path | None = None   # temp file deleted by scaffold_cleanup
    n_split: int | None = None

    # Set by extract_exam_question_numbers (step 17).
    scaffold_nodes: list[dict] = field(default_factory=list)
    raw_layout: dict | None = None

    # Set by extract_exam_questions (step 18) — also rehydrated on resume.
    raw_questions: list[dict] = field(default_factory=list)

    # Set by detect_mark_scheme_graphics (step 20).
    graphics_by_qnum: dict | None = None

    # Set by assign_scheme_questions (step 21).
    questions_per_page: dict | None = None

    # Set by parse_mark_scheme (step 22), consumed by transcribe_scheme_graphics
    # (step 23) and create_report (step 24).
    scheme_data: dict | None = None
