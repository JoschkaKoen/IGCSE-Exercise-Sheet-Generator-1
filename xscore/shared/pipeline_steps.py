"""Step registry + wrapper — declarative core of the xScore pipeline.

The ``STEPS`` tuple below is the canonical ordering and naming of every
pipeline step. Step bodies live in :mod:`xscore.steps` (one module per phase);
:func:`wire_step_fns` looks each one up by name at startup and slots it into
the registry. Each entry's ``number`` matches the ``NN_`` prefix on its
artifact folder under ``output/xscore/<exam>/<timestamp>/``.

Per-step responsibilities (skip-if-from-step, stop-after, timing, exception
routing) are handled inside :func:`run_step`, so step bodies can stay focused
on their actual work.

Usage:

    from xscore.shared.pipeline_steps import run_step, STEPS, wire_step_fns

    wire_step_fns()   # idempotent; once at startup
    for step in STEPS:
        run_step(ctx, step)
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
        ``parse_grading_instructions`` and ``locate_exam_folder`` carry this
        flag so resume can bootstrap ``ctx.instruction``, ``ctx.folder``, and
        ``ctx.artifact_dir`` before later steps short-circuit.
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
# Step ``number`` matches the ``NN_`` prefix on artifact folder names in
# ``xscore/shared/step_folders.py``. Gaps in the numbering reflect the live
# pipeline (locate_exam_folder and prepare_scans don't always have artifact
# folders; they're transparent operations on the scan PDF).

STEPS: tuple[Step, ...] = (
    Step(1,  "parse_grading_instructions", writes=("01_parse_grading_instructions/*",),
         title="Interpret prompt", section="Prompt, folder & roster", bootstrap=True),
    Step(2,  "locate_exam_folder",
         title="Select exam folder", bootstrap=True),
    Step(3,  "read_student_list",          writes=("03_read_student_list/*",),
         title="Read student list"),
    Step(4,  "prepare_scans",              writes=("04_merge_duplex_scans/*",),
         title="Orient and merge scans", section="Scan cleaning"),
    Step(5,  "detect_blank_pages",         writes=("05_detect_blank_pages/*",),
         title="Detect white pages in scanned exam"),
    Step(6,  "autorotate",                 writes=("06_autorotate/*",),
         title="Autorotate scanned exam pages"),
    Step(7,  "deskew",                     writes=("07_deskew/*",),
         title="Deskew scanned pages"),
    # Empty-exam analysis (no scan dependency) — pulled up so problems with
    # the empty exam PDF surface early.
    Step(8,  "detect_exam_layout",         writes=("08_detect_exam_layout/*",),
         title="Detect empty exam layout", section="Empty-exam analysis"),
    Step(9,  "cut_exam_pdf",               writes=("09_cut_exam/*",),
         title="Cut empty exam"),
    # Cover detection + scan geometry.
    Step(10, "cover_page_empty_exam",      writes=("10_cover_page_empty/*",),
         title="Detect cover page in empty exam", section="Geometry & validation"),
    Step(11, "cover_page_scan_first",      writes=("11_cover_page_scan/*",),
         title="Detect cover page in scanned exam"),
    Step(12, "exam_geometry",              writes=("12_exam_geometry/*",),
         title="Calculate number of scanned exam pages per student"),
    # Per-page vision classification — drives student_names and page_order_check.
    Step(13, "student_handwriting_check",  writes=("13_student_handwriting/*",),
         title="Vision classify each scan page (handwriting + page# + cover)"),
    # Cover-anchored student-name detection (consumes student_handwriting_check's covers).
    Step(14, "student_names",              writes=("14_student_names/*",),
         title="Detect student names"),
    # Heuristic page-order check (consumes student_handwriting_check's page numbers).
    Step(15, "page_order_check",           writes=("15_page_order/*",),
         title="Check page order"),
    # Empty-exam blank pages (text-only LLM call).
    Step(16, "exam_blank_detection",       writes=("16_exam_blank_detection/*",),
         title="Detect blank pages in empty exam"),
    # Pure data transform: combines handwriting + names + exam_blank_detection
    # into the marking-page register v1.
    Step(17, "build_marking_register_v1",  writes=("17_build_marking_register/*",),
         title="Build marking page register"),
    # Scaffold split: phase A = detect structure; phase B = fill text.
    Step(18, "detect_exam_scaffold",       writes=("18_detect_exam_scaffold/*",),
         title="Detect exam scaffold (structure)", section="Exam & mark scheme parsing"),
    Step(19, "fill_exam_scaffold",         writes=("19_fill_exam_scaffold/*",),
         title="Fill exam scaffold (text + options)"),
    Step(20, "detect_cross_page_context",  resumable=True,
         writes=(
             "20_detect_cross_page_context/*",
             "19_detect_cross_page_context/*",   # legacy folder (pre-renumber)
             "19_detect_cross_page_figures/*",   # legacy folder (pre-rename)
         ),
         title="Detect cross-page context"),
    Step(21, "detect_mark_scheme_graphics", writes=("21_detect_mark_scheme_graphics/*",),
         title="Detect mark scheme graphics"),
    Step(22, "assign_scheme_questions",    writes=("22_assign_scheme_questions/*",),
         title="Assign questions to mark scheme pages"),
    Step(23, "parse_mark_scheme",          writes=("23_parse_mark_scheme/*",),
         title="Parse mark scheme"),
    Step(24, "create_report",              writes=("24_create_report/*",),
         title="Build grading scaffold"),
    Step(25, "ai_marking_blueprints",      resumable=True,
         writes=("25_ai_marking_blueprints/*",),
         title="Build AI marking blueprints", section="AI marking"),
    Step(26, "extract_student_answers",    resumable=True,
         writes=("26_extract_student_answers/*",),
         title="Extract student answers (transcribe-only pass)"),
    Step(27, "ai_marking",                 resumable=True,
         writes=(
             "27_ai_marking/*",
             "26_ai_marking/*",  # legacy folder (pre-extract-answers refactor)
         ),
         title="Run AI marking"),
    Step(28, "per_student_reports",        resumable=True,
         writes=(
             "28_student_report_preparation/*",
             "27_student_report_preparation/*",  # legacy folder
         ),
         title="Fuse AI marking output to student reports", section="Reports & PDFs"),
    Step(29, "class_stats_curve",          resumable=True,
         writes=(
             "29_class_stats/*",
             "28_class_stats/*",  # legacy folder
         ),
         title="Compute class statistics + curve"),
    Step(30, "per_student_pdfs",           resumable=True,
         writes=(
             "30_student_pdfs/*",
             "29_student_pdfs/*",  # legacy folder
         ),
         title="Generate per-student reports (landscape + portrait + 2UP)"),
    Step(31, "class_report",               resumable=True,
         writes=(
             "31_class_report/*",
             "30_class_report/*",  # legacy folder
         ),
         title="Generate class report"),
    Step(32, "review_queue",               resumable=True,
         writes=(
             "32_review_queue/*",
             "31_review_queue/*",  # legacy folder
         ),
         title="Build review queue"),
    Step(33, "timing_summary",
         writes=(
             "33_timing_summary/*",
             "32_timing_summary/*",  # legacy folder
         ),
         title="Summarise step timings", section="Summary"),
    Step(34, "accuracy_evaluation",
         writes=(
             "34_accuracy/*",
             "33_accuracy/*",  # legacy folder
         ),
         title="Evaluate marking accuracy"),
    Step(35, "ai_costs",
         writes=(
             "35_ai_costs/*",
             "34_ai_costs/*",  # legacy folder
         ),
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
        if di or do or dt:
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
       ``step.bootstrap`` is True (the bootstrap steps must always run to populate ctx).
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
       (cover_page_scan_first, student_handwriting_check, and student_names today).
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

    # Each phase module → tuple of step names (matching ``step.name`` in STEPS
    # and the function attribute name in the module).
    phase_specs: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("xscore.steps.prelude", (
            "parse_grading_instructions",
            "locate_exam_folder",
        )),
        ("xscore.steps.scan", (
            "read_student_list",
            "prepare_scans",
            "detect_blank_pages",
            "autorotate",
            "deskew",
        )),
        ("xscore.steps.geometry", (
            "cover_page_empty_exam",
            "cover_page_scan_first",
            "exam_geometry",
            "student_handwriting_check",
            "student_names",
            "page_order_check",
            "exam_blank_detection",
            "build_marking_register_v1",
        )),
        ("xscore.steps.scaffold", (
            "detect_exam_layout",
            "cut_exam_pdf",
            "detect_exam_scaffold",
            "fill_exam_scaffold",
            "detect_cross_page_context",
            "detect_mark_scheme_graphics",
            "assign_scheme_questions",
            "parse_mark_scheme",
            "create_report",
        )),
        ("xscore.steps.marking", (
            "ai_marking_blueprints",
            "extract_student_answers",
            "ai_marking",
        )),
        ("xscore.steps.reports", (
            "per_student_reports",
            "class_stats_curve",
            "per_student_pdfs",
            "class_report",
            "review_queue",
        )),
        ("xscore.steps.summary", (
            "timing_summary",
            "accuracy_evaluation",
            "ai_costs",
        )),
    )

    fns: dict[str, Callable[["_Ctx"], None]] = {}
    for module_name, step_names in phase_specs:
        try:
            mod = importlib.import_module(module_name)
        except ImportError:
            continue   # phase not yet migrated — leave _unmigrated stubs in place
        for step_name in step_names:
            fn = getattr(mod, step_name, None)
            if fn is not None:
                fns[step_name] = fn

    STEPS = tuple(
        replace(s, fn=fns[s.name]) if s.fn is _unmigrated and s.name in fns else s
        for s in STEPS
    )
