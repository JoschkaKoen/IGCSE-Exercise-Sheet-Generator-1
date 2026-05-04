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
        Currently the cross-page-context and AI-marking-onward steps qualify
        (see ``resumable=True`` annotations on STEPS entries below).
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
    phase:
        Group key used by the runner to apply the right runtime gate. The
        runner walks STEPS in order and dispatches per-step on this field
        rather than hardcoding step-number tuples (so renumbering can't
        misalign the orchestration). Recognised phases:

        * ``None`` — runner does not iterate this step (handled by a special
          helper, e.g. ``scan_phases`` for scan-cleaning, or a direct call
          for the bootstrap/roster steps).
        * ``"empty_exam"`` — gated on ``scaffold_inited``.
        * ``"cover_geometry"`` — gated on ``ctx.cleaned_pdf``.
        * ``"scaffold_phase_b"`` — gated on ``scaffold_inited``.
        * ``"marking_reports_summary"`` — gated on ``ctx.cleaned_pdf and ctx.scaffold``.
    """

    number: int
    name: str
    fn: Callable[["_Ctx"], None] = _unmigrated
    resumable: bool = False
    writes: tuple[str, ...] = field(default_factory=tuple)
    title: str = ""
    section: str | None = None
    bootstrap: bool = False
    phase: str | None = None


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
         title="Detect empty exam layout", section="Empty-exam analysis",
         phase="empty_exam"),
    Step(9,  "cut_exam_pdf",               writes=("09_cut_exam/*",),
         title="Cut empty exam",
         phase="empty_exam"),
    # Cover detection + scan geometry.
    Step(10, "cover_page_empty_exam",      writes=("10_cover_page_empty/*",),
         title="Detect cover page in empty exam", section="Geometry & validation",
         phase="cover_geometry"),
    Step(11, "cover_page_scan_first",      writes=("11_cover_page_scan/*",),
         title="Detect cover page in scanned exam",
         phase="cover_geometry"),
    Step(12, "exam_geometry",              writes=("12_exam_geometry/*",),
         title="Calculate number of scanned exam pages per student",
         phase="cover_geometry"),
    # Two-tier subject detection (filename heuristic → Gemini AI fallback on
    # cover + page 2 of the empty exam). Sets ctx.subject; gates the
    # CODE_FORMATTING prompt section in extract_exam_question_numbers,
    # extract_exam_questions, parse_mark_scheme, ai_marking, extract_student_answers.
    Step(13, "detect_subject",             writes=("13_detect_subject/*",),
         title="Detect exam subject (filename → AI fallback)",
         phase="cover_geometry"),
    # Empty-exam page classification (vision LLM, runs once per exam).
    # Builds the catalog used by step 15's matcher and step 21's continuation pass.
    Step(14, "classify_empty_exam_pages",
         writes=("14_empty_exam_classification/*",),
         title="Classify empty-exam pages (cover/instruction/question/blank/writing-space)",
         phase="cover_geometry"),
    # Per-scan-page vision classification — drives student_names and page_order_check.
    Step(15, "student_handwriting_check",
         writes=("15_student_handwriting/*",
                 "14_student_handwriting/*",   # legacy: pre-step-14-split
                 "13_student_handwriting/*"),  # legacy: pre-detect_subject
         title="Match each scan page against empty exam (page type + page# + handwriting)",
         phase="cover_geometry"),
    # Cover-anchored student-name detection (consumes student_handwriting_check's covers).
    # Sets ctx.page_assignments — the runner kicks off background pre-rendering
    # immediately after this step (see kick_off_render_bg in pipeline/runner.py).
    Step(16, "student_names",
         writes=("16_student_names/*",
                 "15_student_names/*",   # legacy: pre-step-14-split
                 "14_student_names/*"),  # legacy: pre-detect_subject
         title="Detect student names",
         phase="cover_geometry"),
    # Heuristic page-order check (consumes student_handwriting_check's page numbers).
    Step(17, "page_order_check",
         writes=("17_page_order/*",
                 "16_page_order/*",   # legacy: pre-step-14-split
                 "15_page_order/*"),  # legacy: pre-detect_subject
         title="Check page order",
         phase="cover_geometry"),
    # Pure data transform: combines handwriting + names into the marking-page
    # register v1. One primary call per non-cover scan page that has handwriting;
    # continuation/figure/parent extras are added by step 21.
    Step(18, "build_marking_register_v1",
         writes=("18_build_marking_register/*",
                 "17_build_marking_register/*"),  # legacy: pre-step-14-split
         title="Build marking page register",
         phase="cover_geometry"),
    # Empty-exam parse split: question numbers (cheap call) + per-question text (per-page parallel).
    Step(19, "extract_exam_question_numbers",
         writes=("19_extract_exam_question_numbers/*",),
         title="Extract question numbers from empty exam",
         section="Exam & mark scheme parsing",
         phase="scaffold_phase_b"),
    Step(20, "extract_exam_questions",
         writes=("20_extract_exam_questions/*",),
         title="Extract questions from empty exam",
         phase="scaffold_phase_b"),
    Step(21, "detect_cross_page_context",  resumable=True,
         writes=(
             "21_detect_cross_page_context/*",
             "20_detect_cross_page_context/*",   # legacy folder (pre-detect_subject)
             "19_detect_cross_page_context/*",   # legacy folder (pre-renumber)
             "19_detect_cross_page_figures/*",   # legacy folder (pre-rename)
         ),
         title="Detect cross-page context",
         phase="scaffold_phase_b"),
    Step(22, "detect_mark_scheme_graphics",
         writes=("22_detect_mark_scheme_graphics/*",
                 "21_detect_mark_scheme_graphics/*"),  # legacy
         title="Detect mark scheme graphics",
         phase="scaffold_phase_b"),
    Step(23, "assign_scheme_questions",
         writes=("23_assign_scheme_questions/*",
                 "22_assign_scheme_questions/*"),  # legacy
         title="Assign questions to mark scheme pages",
         phase="scaffold_phase_b"),
    Step(24, "parse_mark_scheme",
         writes=("24_parse_mark_scheme/*",
                 "23_parse_mark_scheme/*"),  # legacy
         title="Parse mark scheme",
         phase="scaffold_phase_b"),
    Step(25, "transcribe_scheme_graphics", resumable=True,
         writes=("25_transcribe_scheme_graphics/*",),
         title="Transcribe mark scheme graphics",
         phase="scaffold_phase_b"),
    Step(26, "create_report",
         writes=(
             "26_create_report/*",
             "25_create_report/*",  # legacy folder (pre-transcribe_scheme_graphics)
             "24_create_report/*",  # legacy folder (pre-detect_subject)
         ),
         title="Build grading scaffold",
         phase="scaffold_phase_b"),
    Step(27, "ai_marking_blueprints",      resumable=True,
         writes=(
             "27_ai_marking_blueprints/*",
             "26_ai_marking_blueprints/*",  # legacy folder (pre-transcribe_scheme_graphics)
             "25_ai_marking_blueprints/*",  # legacy folder (pre-detect_subject)
         ),
         title="Build AI marking blueprints", section="AI marking",
         phase="marking_reports_summary"),
    Step(28, "extract_student_answers",    resumable=True,
         writes=(
             "28_extract_student_answers/*",
             "27_extract_student_answers/*",  # legacy folder (pre-transcribe_scheme_graphics)
             "26_extract_student_answers/*",  # legacy folder (pre-detect_subject)
         ),
         title="Extract student answers (transcribe-only pass)",
         phase="marking_reports_summary"),
    Step(29, "ai_marking",                 resumable=True,
         writes=(
             "29_ai_marking/*",
             "28_ai_marking/*",  # legacy folder (pre-transcribe_scheme_graphics)
             "27_ai_marking/*",  # legacy folder (pre-detect_subject)
             "26_ai_marking/*",  # legacy folder (pre-extract-answers refactor)
         ),
         title="Run AI marking",
         phase="marking_reports_summary"),
    Step(30, "per_student_reports",        resumable=True,
         writes=(
             "30_student_report_preparation/*",
             "29_student_report_preparation/*",  # legacy folder (pre-transcribe_scheme_graphics)
             "28_student_report_preparation/*",  # legacy folder (pre-detect_subject)
             "27_student_report_preparation/*",  # legacy folder
         ),
         title="Fuse AI marking output to student reports", section="Reports & PDFs",
         phase="marking_reports_summary"),
    Step(31, "class_stats_curve",          resumable=True,
         writes=(
             "31_class_stats/*",
             "30_class_stats/*",  # legacy folder (pre-transcribe_scheme_graphics)
             "29_class_stats/*",  # legacy folder (pre-detect_subject)
             "28_class_stats/*",  # legacy folder
         ),
         title="Compute class statistics + curve",
         phase="marking_reports_summary"),
    Step(32, "per_student_pdfs",           resumable=True,
         writes=(
             "32_student_pdfs/*",
             "31_student_pdfs/*",  # legacy folder (pre-transcribe_scheme_graphics)
             "30_student_pdfs/*",  # legacy folder (pre-detect_subject)
             "29_student_pdfs/*",  # legacy folder
         ),
         title="Generate per-student reports (landscape + portrait + 2UP)",
         phase="marking_reports_summary"),
    Step(33, "class_report",               resumable=True,
         writes=(
             "33_class_report/*",
             "32_class_report/*",  # legacy folder (pre-transcribe_scheme_graphics)
             "31_class_report/*",  # legacy folder (pre-detect_subject)
             "30_class_report/*",  # legacy folder
         ),
         title="Generate class report",
         phase="marking_reports_summary"),
    Step(34, "review_queue",               resumable=True,
         writes=(
             "34_review_queue/*",
             "33_review_queue/*",  # legacy folder (pre-transcribe_scheme_graphics)
             "32_review_queue/*",  # legacy folder (pre-detect_subject)
             "31_review_queue/*",  # legacy folder
         ),
         title="Build review queue",
         phase="marking_reports_summary"),
    Step(35, "timing_summary",
         writes=(
             "35_timing_summary/*",
             "34_timing_summary/*",  # legacy folder (pre-transcribe_scheme_graphics)
             "33_timing_summary/*",  # legacy folder (pre-detect_subject)
             "32_timing_summary/*",  # legacy folder
         ),
         title="Summarise step timings", section="Summary",
         phase="marking_reports_summary"),
    Step(37, "ai_costs",
         writes=(
             "37_ai_costs/*",
             "36_ai_costs/*",  # legacy folder (pre-transcribe_scheme_graphics)
             "35_ai_costs/*",  # legacy folder (pre-detect_subject)
             "34_ai_costs/*",  # legacy folder
         ),
         title="Summarise AI costs",
         phase="marking_reports_summary"),
)


def step_by_number(n: int) -> Step:
    """Return the registered Step with number *n*.

    Raises ``ValueError`` if no such step exists. Callers that want a soft
    lookup should iterate ``STEPS`` directly — the runner and resume helpers
    rely on this raising so a typo or stale number fails loudly instead of
    crashing inside ``run_step`` with ``AttributeError: 'NoneType' …``.
    """
    for s in STEPS:
        if s.number == n:
            return s
    raise ValueError(f"unknown step number {n}")


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


def _emit_step_event(
    ctx: "_Ctx",
    step: Step,
    *,
    status: str,
    duration_s: float | None,
    error: str | None = None,
) -> None:
    """Fan a step transition out to ``ctx.on_step_event`` if one is registered.

    Observer faults are swallowed (matching ``log_step_event`` resilience).
    ``BaseException`` is intentionally not caught — KeyboardInterrupt and
    SystemExit must still propagate.
    """
    cb = getattr(ctx, "on_step_event", None)
    if cb is None:
        return
    try:
        cb({
            "step_number": step.number,
            "step_name":   step.name,
            "status":      status,
            "duration_s":  duration_s,
            "artifact_dir": str(ctx.artifact_dir) if ctx.artifact_dir else None,
            "error":       error,
        })
    except Exception:
        pass


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
       ``SystemExit`` is also captured here (treated as a step failure) so that
       strict-mode aborts inside steps still write a run-log entry before
       propagating.
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
    _emit_step_event(ctx, step, status="running", duration_s=None)

    usage_before = get_run_usage()
    call_stats_before = get_run_call_stats()
    t0 = time.perf_counter()
    try:
        step.fn(ctx)
    except _EarlyExit:
        _record_step_token_delta(ctx, step.name, usage_before)
        _record_step_call_delta(ctx, step.name, call_stats_before)
        raise
    except (Exception, SystemExit) as exc:
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
        _emit_step_event(ctx, step, status="error", duration_s=elapsed, error=err_str)
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
        _emit_step_event(ctx, step, status="ok", duration_s=elapsed)

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
            "detect_subject",
            "classify_empty_exam_pages",
            "student_handwriting_check",
            "student_names",
            "page_order_check",
            "build_marking_register_v1",
        )),
        ("xscore.steps.scaffold", (
            "detect_exam_layout",
            "cut_exam_pdf",
            "extract_exam_question_numbers",
            "extract_exam_questions",
            "detect_cross_page_context",
            "detect_mark_scheme_graphics",
            "assign_scheme_questions",
            "parse_mark_scheme",
            "transcribe_scheme_graphics",
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
            "ai_costs",
        )),
    )

    fns: dict[str, Callable[["_Ctx"], None]] = {}
    missing: list[str] = []
    for module_name, step_names in phase_specs:
        try:
            mod = importlib.import_module(module_name)
        except ImportError:
            continue   # phase not yet migrated — leave _unmigrated stubs in place
        for step_name in step_names:
            fn = getattr(mod, step_name, None)
            if fn is None:
                missing.append(f"{module_name}.{step_name}")
                continue
            fns[step_name] = fn
    if missing:
        raise RuntimeError(
            "wire_step_fns: phase modules imported but the following step "
            "functions are missing (rename or registry typo?): "
            + ", ".join(missing)
        )

    STEPS = tuple(
        replace(s, fn=fns[s.name]) if s.fn is _unmigrated and s.name in fns else s
        for s in STEPS
    )
