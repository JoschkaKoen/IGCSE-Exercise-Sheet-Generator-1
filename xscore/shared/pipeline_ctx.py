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
    run_started_at: float = 0.0  # perf_counter() set in _run; consumed by step 28 wall-clock row
    # Steps 19–23: AI marking pipeline
    num_students: int = 0
    pages_per_student: int = 0
    # Per-step wall-clock timings. Written by xScore.py's step bodies and by
    # :func:`xscore.shared.pipeline_steps.run_step` once steps migrate out of
    # xScore.py's nested closures into the registry.
    step_timings: dict[str, float] = field(default_factory=dict)
    # Per-step token usage: step_name → model → {"input": N, "output": N}.
    # Written by run_step as a delta of get_run_usage() across the step body.
    step_token_usage: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    # Per-step API call stats: step_name → model → {"calls": N, "total_duration_s": F}.
    # Written by run_step as a delta of get_run_call_stats() across the step body.
    step_call_stats: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    # Captured exceptions per step (also re-raised by ``run_step``). Used by
    # the run-manifest writer to distinguish "ran but errored" from "did not run".
    step_failures: list[dict] = field(default_factory=list)
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
    student_filter: list[str] | None = None  # --student; restrict marking + reports to these names (lower-case)
    geo: dict[str, Any] = field(default_factory=dict)   # scan geometry from step 8; updated by step 11
    b64_future: "Future[dict[int, str]] | None" = None  # render_pages_b64 submitted by kick_off_render_bg
    accuracy_summary: dict[str, Any] | None = None      # set by step 29; read by step 30
    scan_match: Path | None = None                      # set by step 4 (or scan_phases single-PDF branch),
                                                        # read by step 5
    scaffold_state: dict[str, Any] = field(default_factory=dict)
    # transient store for steps 15–20 shared locals so individual step bodies stay focused.
    # Holds keys like exam_pdf, answer_pdf, client, fmt, layout_result, layout_elapsed,
    # layout_model, actual_exam_pdf, split_pdf_temp_path, n_split, raw_questions,
    # raw_layout, graphics_by_qnum, scheme_data. Cleared by scaffold_phase finally.
    # --- Cross-step state for steps 23–27 (split out of compile_reports) ---
    # Set by step 23 (per-student reports), consumed by 24 (curve), 25 (PDFs), 26 (class), 27 (review).
    student_summaries: list[dict] | None = None
    full_reports: dict[str, dict] | None = None
    q_totals: dict[str, list[float]] | None = None

    def __post_init__(self) -> None:
        # All four fields are guaranteed by parse_args() in xScore.py.
        if self.args.stop_after is not None:
            self.stop_after = self.args.stop_after
        if self.args.from_step is not None:
            self.from_step = self.args.from_step
        if self.args.resume_dir is not None:
            self.resume_dir = self.args.resume_dir
        # --student supports both repeated flags (action="append") and a single
        # comma-separated value (--student "Alice, Bob"). Normalise to lowercase
        # exact-match keys and drop empty entries.
        if self.args.student:
            names: list[str] = []
            for entry in self.args.student:
                for piece in entry.split(","):
                    piece = piece.strip().lower()
                    if piece:
                        names.append(piece)
            self.student_filter = names or None
