"""Pipeline context dataclass and early-exit sentinel.

Kept here so pipeline internals are importable without triggering xScore.py's
_Tee logging setup.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xscore.shared.models import ExamScaffold, TaskInstruction


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
    # Steps 12–16: AI marking pipeline
    num_students: int = 0
    pages_per_student: int = 0
    step_timings_marking: dict[str, float] = field(default_factory=dict)
    marking_api_calls: list[dict] = field(default_factory=list)
    marking_failures: list[dict] = field(default_factory=list)
    page_assignments: list | None = None     # list[PageAssignment] set by step 8
    cover_page_mode: bool = False            # True when step 8 detects cover pages in the scan
    empty_exam_has_cover: bool | None = None  # set by step 8b; None = check not performed
    step_offset: int = 0                     # 1 when split-subpages mode adds step 9 (layout + cut)
    stop_after: int = 9999                   # --stop-after N; 9999 = run everything
    from_step: int | None = None             # --from-step N; skip steps < N, resume from prior run
    resume_dir: Path | None = None           # --resume-dir PATH; prior artifact dir to resume from
    b64_future: Any = None                   # Future[dict] set by _kick_off_render_bg after step 8

    def __post_init__(self) -> None:
        if getattr(self.args, "stop_after", None) is not None:
            self.stop_after = self.args.stop_after
        if getattr(self.args, "from_step", None) is not None:
            self.from_step = self.args.from_step
        if getattr(self.args, "resume_dir", None) is not None:
            self.resume_dir = self.args.resume_dir
