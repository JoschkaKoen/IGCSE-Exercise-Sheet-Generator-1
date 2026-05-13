"""Pipeline context dataclass and early-exit sentinel.

Kept here so pipeline internals are importable without triggering XScore.py's
_Tee logging setup.
"""

from __future__ import annotations

import argparse
from concurrent.futures import Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from xscore.shared.models import ExamScaffold, PageAssignment, TaskInstruction
    from xscore.shared.subjects import Subject


class _EarlyExit(Exception):
    """Pipeline stopped because --stop-after N was reached."""


@dataclass
class _Ctx:
    args: argparse.Namespace
    timestamp: str
    instruction: "TaskInstruction | None" = None
    parse_elapsed: float = 0.0
    # Captured by parse_grading_instructions (parse_prompt out=) and persisted
    # by locate_exam_folder once artifact_dir exists. Keys: "model", "system",
    # "user", "raw", "thinking".
    parse_prompt_debug: dict | None = None
    force_clean_scan: bool = False
    folder: Path | None = None
    artifact_dir: Path | None = None
    students: list[str] | None = None
    scaffold: "ExamScaffold | None" = None
    cleaned_pdf: Path | None = None
    pipeline_completed_ok: bool = False
    run_started_at: float = 0.0  # perf_counter() set in _run; consumed by the timing_summary wall-clock row
    # AI-marking pipeline state
    num_students: int = 0
    pages_per_student: int = 0
    # Per-step wall-clock timings. Written by XScore.py's step bodies and by
    # :func:`xscore.shared.pipeline_steps.run_step` once steps migrate out of
    # XScore.py's nested closures into the registry.
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
    page_assignments: "list[PageAssignment] | None" = None  # set by student_names
    # --- Cover page detection ---
    # Set by cover_page_empty_exam; None means the AI check was skipped (no API key or error).
    empty_exam_has_cover: bool | None = None
    # Set by cover_page_scan_first from the scan page-1 check; final once that
    # step finishes (no retroactive update). False = no cover (also the default).
    cover_page_mode: bool = False
    # Set by detect_subject (detect_subject). None means the step hasn't run yet (or
    # was skipped on resume from a pre-detect_subject run); downstream prompts treat
    # None as "no code formatting" via :func:`xscore.shared.subjects.needs_code_formatting`.
    subject: "Subject | None" = None
    stop_after: int = 9999                   # --stop-after N; 9999 = run everything
    from_step: int | None = None             # --from-step N; skip steps < N, resume from prior run
    resume_dir: Path | None = None           # --resume-dir PATH; prior artifact dir to resume from
    student_filter: list[str] | None = None  # --student; restrict marking + reports to these names (lower-case)
    limit_students: int | None = None        # --limit-students N; slice raw_assignments to first N (after other filters)
    geo: dict[str, Any] = field(default_factory=dict)   # scan geometry from exam_geometry
    b64_future: "Future[dict[int, str]] | None" = None  # render_pages_b64 submitted by kick_off_render_bg
    scan_match: Path | None = None                      # set by prepare_scans (or scan_phases single-PDF
                                                        # branch), read by detect_blank_pages
    scaffold_state: dict[str, Any] = field(default_factory=dict)
    # transient store for shared locals across the scaffold-building steps.
    # Holds keys like exam_pdf, answer_pdf, client, fmt, layout_result, layout_elapsed,
    # layout_model, actual_exam_pdf, split_pdf_temp_path, n_split, raw_questions,
    # raw_layout, graphics_by_qnum, questions_per_page, scheme_data. Cleared by
    # scaffold_phase finally.
    # --- Cross-step state for the report-pipeline tail (split out of compile_reports) ---
    # Set by per_student_reports, consumed by class_stats_curve, per_student_pdfs,
    # class_report, and review_queue.
    student_summaries: list[dict] | None = None
    # Per-student reports. When the marking page register knows about skipped
    # scan pages with questions, the report stored here is the *augmented*
    # version — those questions appear inline as ``(not answered)`` rows so
    # the regular per-student PDF/MD/XML lists every gradable item.
    full_reports: dict[str, dict] | None = None
    q_totals: dict[str, list[float]] | None = None
    # Failure surfaces for the report-pipeline tail. Populated by
    # per_student_reports (merge), surfaced by per_student_reports / per_student_pdfs,
    # persisted by review_queue.
    failed_students: list[dict] = field(default_factory=list)
    mark_collisions: list[dict] = field(default_factory=list)
    # Optional per-step observer. Set by callers that want progress events
    # without parsing stdout (the FastAPI web grade page is the first user).
    # Receives a dict per event: {step_number, step_name, status, duration_s,
    # artifact_dir, error}. ``run_step`` invokes this alongside its existing
    # ``log_step_event`` writes; observer faults are swallowed so a misbehaving
    # consumer can never crash the pipeline.
    on_step_event: "Callable[[dict], None] | None" = None

    def __post_init__(self) -> None:
        # All four fields are guaranteed by parse_args() in XScore.py.
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
        if getattr(self.args, "limit_students", None):
            self.limit_students = self.args.limit_students
