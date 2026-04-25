"""Step registry + wrapper — declarative core for the xScore pipeline.

This module provides the *infrastructure* for moving xScore.py's 25 nested
``_stepNN_*`` closures into a flat registry. Migration is incremental: each
step body can be lifted out into a top-level ``step_NN_xxx(ctx)`` function
and registered here without changing any other steps.

Today's xScore.py still hard-wires the step ordering inside ``_run``; the
registry below mirrors that ordering so callers (e.g. the run-log writer in
:mod:`xscore.shared.run_log`) can ask "which step is N?" without parsing
xScore.py source. As steps are migrated, replace the ``fn=_unmigrated`` stub
with a real callable.

Usage when a step has been migrated:

    from xscore.shared.pipeline_steps import run_step, STEPS

    for step in STEPS:
        if step.fn is _unmigrated:
            continue   # still inlined in xScore.py — leave it there
        run_step(ctx, step)

Each step's responsibilities (skip-if-from-step, stop-after, timing,
exception routing) are handled inside :func:`run_step`, eliminating the
~20 duplicated guards currently scattered through xScore.py.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from xscore.shared.pipeline_ctx import _Ctx


def _unmigrated(ctx: "_Ctx") -> None:  # pragma: no cover - placeholder
    """Sentinel for steps still inlined in xScore.py. See module docstring."""
    raise NotImplementedError(
        "This step is still implemented inline in xScore.py. "
        "Lift its body out and register the function here."
    )


@dataclass(frozen=True)
class Step:
    """Descriptor for one pipeline step.

    Attributes
    ----------
    number:
        1-based step ordinal (matches the artifact folder prefix).
    name:
        Short snake-case identifier used as the key in ``ctx.step_timings``
        and as the ``step_name`` field in run-log entries.
    fn:
        Step body. Receives the ``_Ctx`` and mutates it in place; returns
        nothing. Steps that are still inlined in xScore.py keep
        ``fn=_unmigrated``.
    resumable:
        True iff a prior run's artifacts can be reused starting from this step.
        Today only blueprints (21), marking (22), and reports (23) qualify.
    writes:
        Globs (relative to ``ctx.artifact_dir``) the step writes — informational
        only; used by the resume-artifact copier and (eventually) by the
        run-log writer to record what the step produced.
    """

    number: int
    name: str
    fn: Callable[["_Ctx"], None] = _unmigrated
    resumable: bool = False
    writes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
#
# Order matters — consumers iterate this list to determine pipeline ordering.
# Numbers are kept aligned with the artifact-folder prefixes in
# `xscore/shared/exam_paths.py` (STEP_01, STEP_03, …, STEP_24). Gaps in the
# numbering reflect the live pipeline (steps 2 and 4 don't have artifact
# folders today; they're transparent operations on the scan PDF).

STEPS: tuple[Step, ...] = (
    Step(1,  "parse_grading_instructions", writes=("01_parse_grading_instructions/*",)),
    Step(2,  "locate_exam_folder"),
    Step(3,  "read_student_list",          writes=("03_read_student_list/*",)),
    Step(4,  "merge_duplex_scan_halves"),
    Step(5,  "detect_blank_pages",         writes=("05_detect_blank_pages/*",)),
    Step(6,  "autorotate",                 writes=("06_autorotate/*",)),
    Step(7,  "deskew",                     writes=("07_deskew/*",)),
    Step(8,  "exam_geometry",              writes=("08_exam_geometry/*",)),
    Step(9,  "cover_page_empty_exam",      writes=("09_cover_page/*",)),
    Step(10, "cover_page_scan",            writes=("10_cover_page_scan/*",)),
    Step(11, "student_names",              writes=("11_student_names/*",)),
    Step(12, "page_count_validation"),
    Step(13, "page_order_check",           writes=("13_page_order/*",)),
    Step(14, "blank_page_detection",       writes=("14_blank_pages/*",)),
    Step(15, "detect_exam_layout",         writes=("15_detect_exam_layout/*",)),
    Step(16, "cut_exam_pdf",               writes=("16_cut_exam/*",)),
    Step(17, "parse_exam_pdf",             writes=("17_parse_exam_pdf/*",)),
    Step(18, "detect_mark_scheme_graphics",writes=("18_detect_mark_scheme_graphics/*",)),
    Step(19, "parse_mark_scheme",          writes=("19_parse_mark_scheme/*",)),
    Step(20, "create_report",              writes=("20_create_report/*",)),
    Step(21, "ai_marking_blueprints",      resumable=True,
         writes=("21_ai_marking_blueprints/*",)),
    Step(22, "ai_marking",                 resumable=True,
         writes=("22_ai_marking/*",)),
    Step(23, "per_student_reports",        resumable=True,
         writes=("23_student_reports/*",)),
    Step(24, "class_stats_curve",          resumable=True,
         writes=("24_class_stats/*",)),
    Step(25, "per_student_pdfs",           resumable=True,
         writes=("25_student_pdfs/*",)),
    Step(26, "class_report",               resumable=True,
         writes=("26_class_report/*",)),
    Step(27, "review_queue",               resumable=True,
         writes=("27_review_queue/*",)),
    Step(28, "timing_summary",             writes=("28_timing_summary/*",)),
    Step(29, "accuracy_evaluation",        writes=("29_accuracy/*",)),
    Step(30, "ai_costs",                   writes=("30_ai_costs/*",)),
)


def step_by_number(n: int) -> Step | None:
    for s in STEPS:
        if s.number == n:
            return s
    return None


def step_by_name(name: str) -> Step | None:
    for s in STEPS:
        if s.name == name:
            return s
    return None


def resumable_step_numbers() -> tuple[int, ...]:
    """Step numbers that ``--from-step N`` accepts today."""
    return tuple(s.number for s in STEPS if s.resumable)


def max_step_number() -> int:
    return max(s.number for s in STEPS)


# ---------------------------------------------------------------------------
# Per-step wrapper
# ---------------------------------------------------------------------------

def run_step(ctx: "_Ctx", step: Step) -> None:
    """Run *step* under the unified guard set.

    Responsibilities (kept in one place so individual steps don't duplicate them):

    1. Skip if ``ctx.from_step > step.number`` (resume past this step).
    2. Print the step header via ``terminal_ui.pipeline_step``.
    3. Time the body with ``time.perf_counter`` and store under
       ``ctx.step_timings[step.name]``.
    4. Honour ``ctx.stop_after``: raise :class:`_EarlyExit` after a step whose
       number equals ``stop_after``. Steps with number > ``stop_after`` are
       skipped without running.
    5. Capture exceptions into ``ctx.step_failures`` and re-raise — callers
       decide whether to treat it as fatal (most steps) or warn-and-continue
       (steps 9, 13, 14 today).
    """
    from xscore.shared.pipeline_ctx import _EarlyExit
    from xscore.shared.run_log import log_step_event
    from xscore.shared.terminal_ui import pipeline_step

    # --- skip-if-resumed ---
    if ctx.from_step is not None and step.number < ctx.from_step:
        return

    # --- stop-after (run > stop_after means skip outright) ---
    if step.number > ctx.stop_after:
        raise _EarlyExit()

    pipeline_step(step.number, step.name)

    t0 = time.perf_counter()
    try:
        step.fn(ctx)
    except _EarlyExit:
        raise
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        ctx.step_timings[step.name] = elapsed
        err_str = f"{type(exc).__name__}: {exc}"
        ctx.step_failures.append({
            "step_number": step.number,
            "step_name":   step.name,
            "duration_s":  elapsed,
            "error":       err_str,
        })
        log_step_event(
            ctx,
            step_number=step.number, step_name=step.name,
            status="error", duration_s=elapsed, error=err_str,
        )
        raise
    else:
        elapsed = time.perf_counter() - t0
        ctx.step_timings[step.name] = elapsed
        log_step_event(
            ctx,
            step_number=step.number, step_name=step.name,
            status="ok", duration_s=elapsed,
        )

    # If this step IS the stop_after sentinel, end the run cleanly.
    if step.number == ctx.stop_after:
        raise _EarlyExit()
