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
        Today only blueprints (23), marking (24), and reports (25) qualify.
    writes:
        Globs (relative to ``ctx.artifact_dir``) the step writes — informational
        only; used by the resume-artifact copier and (eventually) by the
        run-log writer to record what the step produced.
    title:
        User-facing display string passed to ``pipeline_step``. Falls back to
        a humanised version of ``name`` if empty.
    section:
        If set, ``run_step`` prints ``pipeline_section(section)`` immediately
        before this step's header — used to mark phase boundaries (geometry,
        scaffold, marking, reports, summary).
    bootstrap:
        If True, this step runs unconditionally regardless of ``ctx.from_step``.
        Steps 1 and 2 carry this flag so resume can bootstrap ``ctx.instruction``,
        ``ctx.folder``, ``ctx.artifact_dir`` before later steps short-circuit.
    """

    number: int
    name: str
    fn: Callable[["_Ctx"], None] = _unmigrated
    resumable: bool = False
    writes: tuple[str, ...] = field(default_factory=tuple)
    title: str = ""
    section: str | None = None
    bootstrap: bool = False


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
#
# Order matters — consumers iterate this list to determine pipeline ordering.
# Numbers are kept aligned with the artifact-folder prefixes in
# `xscore/shared/exam_paths.py` (STEP_01, STEP_03, …, STEP_32). Gaps in the
# numbering reflect the live pipeline (steps 2 and 4 don't have artifact
# folders today; they're transparent operations on the scan PDF).

STEPS: tuple[Step, ...] = (
    Step(1,  "parse_grading_instructions", writes=("01_parse_grading_instructions/*",),
         title="Interpret prompt", section="Prompt, folder & roster", bootstrap=True),
    Step(2,  "locate_exam_folder",
         title="Select exam folder", bootstrap=True),
    Step(3,  "read_student_list",          writes=("03_read_student_list/*",),
         title="Read student list"),
    Step(4,  "merge_duplex_scan_halves",
         title="Merge duplex scans", section="Scan cleaning"),
    Step(5,  "detect_blank_pages",         writes=("05_detect_blank_pages/*",),
         title="Detect white pages in scanned exam"),
    Step(6,  "autorotate",                 writes=("06_autorotate/*",),
         title="Autorotate scanned exam pages"),
    Step(7,  "deskew",                     writes=("07_deskew/*",),
         title="Deskew scanned pages"),
    Step(8,  "cover_page_empty_exam",      writes=("08_cover_page_empty/*",),
         title="Detect cover page in empty exam", section="Geometry & validation"),
    Step(9,  "cover_page_scan_first",      writes=("09_cover_page_scan/*",),
         title="Detect cover page in scanned exam"),
    Step(10, "exam_geometry",              writes=("10_exam_geometry/*",),
         title="Calculate number of scanned exam pages per student"),
    Step(11, "cover_page_verify",          writes=("11_cover_page_verify/*",),
         title="Verify cover pages on remaining students"),
    Step(12, "student_names",              writes=("12_student_names/*",),
         title="Detect student names"),
    Step(13, "page_order_check",           writes=("13_page_order/*",),
         title="Check page order"),
    Step(14, "exam_blank_detection",        writes=("14_exam_blank_detection/*",),
         title="Detect blank pages in empty exam"),
    Step(15, "student_handwriting_check",  writes=("15_student_handwriting/*",),
         title="Check student handwriting on blank pages"),
    Step(16, "detect_exam_layout",         writes=("16_detect_exam_layout/*",),
         title="Detect empty exam layout", section="Exam & mark scheme parsing"),
    Step(17, "cut_exam_pdf",               writes=("17_cut_exam/*",),
         title="Cut empty exam"),
    Step(18, "parse_exam_pdf",             writes=("18_parse_exam_pdf/*",),
         title="Parse empty exam PDF"),
    Step(19, "detect_mark_scheme_graphics",writes=("19_detect_mark_scheme_graphics/*",),
         title="Detect mark scheme graphics"),
    Step(20, "assign_scheme_questions",    writes=("20_assign_scheme_questions/*",),
         title="Assign questions to mark scheme pages"),
    Step(21, "parse_mark_scheme",          writes=("21_parse_mark_scheme/*",),
         title="Parse mark scheme"),
    Step(22, "create_report",              writes=("22_create_report/*",),
         title="Build grading scaffold"),
    Step(23, "ai_marking_blueprints",      resumable=True,
         writes=("23_ai_marking_blueprints/*",),
         title="Build AI marking blueprints", section="AI marking"),
    Step(24, "ai_marking",                 resumable=True,
         writes=("24_ai_marking/*",),
         title="Run AI marking"),
    Step(25, "per_student_reports",        resumable=True,
         writes=("25_student_report_preparation/*",),
         title="Fuse AI marking output to student reports", section="Reports & PDFs"),
    Step(26, "class_stats_curve",          resumable=True,
         writes=("26_class_stats/*",),
         title="Compute class statistics + curve"),
    Step(27, "per_student_pdfs",           resumable=True,
         writes=("27_student_pdfs/*",),
         title="Generate per-student reports (landscape + portrait + 2UP)"),
    Step(28, "class_report",               resumable=True,
         writes=("28_class_report/*",),
         title="Generate class report"),
    Step(29, "review_queue",               resumable=True,
         writes=("29_review_queue/*",),
         title="Build review queue"),
    Step(30, "timing_summary",             writes=("30_timing_summary/*",),
         title="Summarise step timings", section="Summary"),
    Step(31, "accuracy_evaluation",        writes=("31_accuracy/*",),
         title="Evaluate marking accuracy"),
    Step(32, "ai_costs",                   writes=("32_ai_costs/*",),
         title="Summarise AI costs"),
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

def _record_step_token_delta(
    ctx: "_Ctx",
    step_name: str,
    usage_before: dict[str, dict[str, int]],
) -> None:
    """Diff get_run_usage() against *usage_before* and store on ctx if non-empty.

    Steps run sequentially (parallelism is contained inside step bodies), so the
    delta correctly attributes all tokens consumed by this step's API calls.
    """
    from eXercise.ai_client import get_run_usage

    after = get_run_usage()
    delta: dict[str, dict[str, int]] = {}
    for model, ac in after.items():
        bc = usage_before.get(model, {"input": 0, "output": 0, "thinking": 0})
        di = ac["input"]  - bc.get("input", 0)
        do = ac["output"] - bc.get("output", 0)
        dt = ac.get("thinking", 0) - bc.get("thinking", 0)
        if di or do:
            delta[model] = {"input": di, "output": do, "thinking": dt}
    if delta:
        ctx.step_token_usage[step_name] = delta


def _record_step_call_delta(
    ctx: "_Ctx",
    step_name: str,
    stats_before: dict[str, dict[str, float]],
) -> None:
    """Diff get_run_call_stats() against *stats_before* and store on ctx if non-empty.

    Parallels :func:`_record_step_token_delta`; gates on call-count delta only
    (a duration without a call count is incoherent).
    """
    from eXercise.ai_client import get_run_call_stats

    after = get_run_call_stats()
    delta: dict[str, dict[str, float]] = {}
    for model, ac in after.items():
        bc = stats_before.get(model, {"calls": 0.0, "total_duration_s": 0.0})
        dc = ac["calls"] - bc["calls"]
        dd = ac["total_duration_s"] - bc["total_duration_s"]
        if dc:
            delta[model] = {"calls": dc, "total_duration_s": dd}
    if delta:
        ctx.step_call_stats[step_name] = delta


def run_step(ctx: "_Ctx", step: Step) -> None:
    """Run *step* under the unified guard set.

    Responsibilities (kept in one place so individual steps don't duplicate them):

    1. Skip if ``ctx.from_step > step.number`` (resume past this step), unless
       ``step.bootstrap`` is True (steps 1–2 must always run to bootstrap ctx).
    2. Print the section header (via ``pipeline_section``) when ``step.section`` is set,
       then the step header via ``terminal_ui.pipeline_step`` (using ``step.title`` if
       set, else a humanised ``step.name``).
    3. Time the body with ``time.perf_counter`` and store under
       ``ctx.step_timings[step.name]``.
    4. Honour ``ctx.stop_after``: raise :class:`_EarlyExit` after a step whose
       number equals ``stop_after``. Steps with number > ``stop_after`` are
       skipped without running.
    5. Capture exceptions into ``ctx.step_failures`` and re-raise — callers
       decide whether to treat it as fatal (most steps) or warn-and-continue
       (steps 9, 13, 14 today).
    """
    from eXercise.ai_client import get_run_call_stats, get_run_usage
    from xscore.shared.pipeline_ctx import _EarlyExit
    from xscore.shared.run_log import log_step_event
    from xscore.shared.terminal_ui import pipeline_section, pipeline_step

    # --- skip-if-resumed (bootstrap steps run regardless) ---
    if (ctx.from_step is not None and step.number < ctx.from_step
            and not step.bootstrap):
        return

    # --- stop-after (run > stop_after means skip outright) ---
    if step.number > ctx.stop_after:
        raise _EarlyExit()

    if step.section is not None:
        pipeline_section(step.section)
    display = step.title or step.name.replace("_", " ").capitalize()
    pipeline_step(step.number, display)

    usage_before = get_run_usage()
    call_stats_before = get_run_call_stats()
    t0 = time.perf_counter()
    try:
        step.fn(ctx)
    except _EarlyExit:
        _record_step_token_delta(ctx, step.name, usage_before)
        _record_step_call_delta(ctx, step.name, call_stats_before)
        raise
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        ctx.step_timings[step.name] = elapsed
        _record_step_token_delta(ctx, step.name, usage_before)
        _record_step_call_delta(ctx, step.name, call_stats_before)
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
        _record_step_token_delta(ctx, step.name, usage_before)
        _record_step_call_delta(ctx, step.name, call_stats_before)
        log_step_event(
            ctx,
            step_number=step.number, step_name=step.name,
            status="ok", duration_s=elapsed,
        )

    # If this step IS the stop_after sentinel, end the run cleanly.
    if step.number == ctx.stop_after:
        raise _EarlyExit()


# ---------------------------------------------------------------------------
# Step-fn wiring
# ---------------------------------------------------------------------------

def wire_step_fns() -> None:
    """Replace ``_unmigrated`` stubs with real step bodies.

    Called once at startup by ``xscore.pipeline.runner.run_pipeline`` after
    ``load_dotenv`` has populated env vars. Idempotent — safe to call again.

    Step modules are imported lazily here so importing :mod:`pipeline_steps`
    for ``STEPS[*].writes`` introspection (e.g. from ``resume.py``) does not
    pull in the entire pipeline at module-load time.

    Phase modules that don't exist yet are silently skipped so the migration
    can roll out one phase at a time without breaking the orchestrator.
    """
    global STEPS
    import importlib
    from dataclasses import replace

    # Each phase module → (module_name, mapping of step name → attribute)
    phase_specs: tuple[tuple[str, dict[str, str]], ...] = (
        ("xscore.steps.prelude", {
            "parse_grading_instructions":   "step_01_parse",
            "locate_exam_folder":           "step_02_folder",
        }),
        ("xscore.steps.scan", {
            "read_student_list":            "step_03_students",
            "merge_duplex_scan_halves":     "step_04_merge",
            "detect_blank_pages":           "step_05_blanks",
            "autorotate":                   "step_06_rotate",
            "deskew":                       "step_07_deskew",
        }),
        ("xscore.steps.geometry", {
            "cover_page_empty_exam":        "step_08_cover_empty",
            "cover_page_scan_first":        "step_09_cover_scan_first",
            "exam_geometry":                "step_10_geometry",
            "cover_page_verify":            "step_11_cover_verify",
            "student_names":                "step_12_student_names",
            "page_order_check":             "step_13_page_order",
            "exam_blank_detection":         "step_14_exam_blank",
            "student_handwriting_check":    "step_15_handwriting",
        }),
        ("xscore.steps.scaffold", {
            "detect_exam_layout":           "step_16_layout",
            "cut_exam_pdf":                 "step_17_cut",
            "parse_exam_pdf":               "step_18_parse_exam",
            "detect_mark_scheme_graphics":  "step_19_scheme_graphics",
            "assign_scheme_questions":      "step_20_assign_questions",
            "parse_mark_scheme":            "step_21_parse_scheme",
            "create_report":                "step_22_create_report",
        }),
        ("xscore.steps.marking", {
            "ai_marking_blueprints":        "step_23_blueprints",
            "ai_marking":                   "step_24_mark",
        }),
        ("xscore.steps.reports", {
            "per_student_reports":          "step_25_per_student_reports",
            "class_stats_curve":            "step_26_class_stats",
            "per_student_pdfs":             "step_27_per_student_pdfs",
            "class_report":                 "step_28_class_report",
            "review_queue":                 "step_29_review_queue",
        }),
        ("xscore.steps.summary", {
            "timing_summary":               "step_30_timing",
            "accuracy_evaluation":          "step_31_accuracy",
            "ai_costs":                     "step_32_costs",
        }),
    )

    fns: dict[str, Callable[["_Ctx"], None]] = {}
    for module_name, mapping in phase_specs:
        try:
            mod = importlib.import_module(module_name)
        except ImportError:
            continue   # phase not yet migrated — leave _unmigrated stubs in place
        for step_name, attr in mapping.items():
            fn = getattr(mod, attr, None)
            if fn is not None:
                fns[step_name] = fn

    STEPS = tuple(
        replace(s, fn=fns[s.name]) if s.fn is _unmigrated and s.name in fns else s
        for s in STEPS
    )
