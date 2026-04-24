"""Pipeline context dataclass and early-exit sentinel.

Kept here so pipeline internals are importable without triggering xScore.py's
_Tee logging setup.
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xscore.shared.models import ExamScaffold, PageAssignment, TaskInstruction


class _EarlyExit(Exception):
    """Pipeline stopped because --stop-after N was reached."""


@dataclass
class _Ctx:
    args: argparse.Namespace
    timestamp: str
    instruction: "TaskInstruction | None" = None
    parse_elapsed: float = 0.0
    force_clean_scan: bool = False
    folder: Path | None = None
    artifact_dir: Path | None = None
    students: list[str] | None = None
    scaffold: "ExamScaffold | None" = None
    cleaned_pdf: Path | None = None
    pipeline_completed_ok: bool = False
    # Steps 19–23: AI marking pipeline
    num_students: int = 0
    pages_per_student: int = 0
    step_timings_marking: dict[str, float] = field(default_factory=dict)
    marking_api_calls: list[dict] = field(default_factory=list)
    marking_failures: list[dict] = field(default_factory=list)
    page_assignments: "list[PageAssignment] | None" = None  # set by step 11
    # --- Cover page detection (steps 9–11) ---
    # Step 9 sets this; None means the AI check was skipped (no API key or error).
    empty_exam_has_cover: bool | None = None
    # Set to a preliminary value by step 10, then finalized by step 11.
    # False = no cover pages found (also the pre-step-10 default).
    cover_page_mode: bool = False
    stop_after: int = 9999                   # --stop-after N; 9999 = run everything
    from_step: int | None = None             # --from-step N; skip steps < N, resume from prior run
    resume_dir: Path | None = None           # --resume-dir PATH; prior artifact dir to resume from
    geo: dict[str, Any] = field(default_factory=dict)   # scan geometry from step 8; updated by step 11
    b64_future: "Future[dict[int, str]] | None" = None  # render_pages_b64 submitted by _kick_off_render_bg
    accuracy_summary: dict[str, Any] | None = None      # set by step 22; read by step 23

    def __post_init__(self) -> None:
        if getattr(self.args, "stop_after", None) is not None:
            self.stop_after = self.args.stop_after
        if getattr(self.args, "from_step", None) is not None:
            self.from_step = self.args.from_step
        if getattr(self.args, "resume_dir", None) is not None:
            self.resume_dir = self.args.resume_dir
