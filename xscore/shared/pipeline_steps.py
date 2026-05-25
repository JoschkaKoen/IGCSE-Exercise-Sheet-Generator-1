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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from xscore.shared.pipeline_ctx import _Ctx


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
        nothing. ``None`` until :func:`wire_step_fns` runs at startup; calling
        ``run_step`` on an unwired ``Step`` raises ``TypeError``.
    resumable:
        True iff a prior run's artifacts can be reused starting from this step.
        Currently the cross-page-context and AI-marking-onward steps qualify
        (see ``resumable=True`` annotations on STEPS entries below).
    _explicit_writes:
        If set, overrides the auto-derived ``writes`` property. ``None`` (the
        default) means "auto-derive a single ``(NN_name/*,)`` glob from
        ``number`` + ``name``"; ``()`` declares a step that writes nothing
        (today only ``locate_exam_folder``). Hidden behind the ``writes``
        property — consumers read ``step.writes``, never this directly.
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
    fn: Callable[["_Ctx"], None] | None = None
    resumable: bool = False
    _explicit_writes: tuple[str, ...] | None = None
    title: str = ""
    section: str | None = None
    bootstrap: bool = False
    phase: str | None = None

    @property
    def writes(self) -> tuple[str, ...]:
        """Globs (relative to ``ctx.artifact_dir``) the step writes.

        Auto-derived from ``number`` and ``name`` as ``(f"{NN}_{name}/*",)``
        when ``_explicit_writes is None`` (the common case — every step folder
        in :mod:`xscore.shared.step_folders` follows that pattern). Set
        ``_explicit_writes=()`` to declare a step that writes nothing.
        """
        if self._explicit_writes is not None:
            return self._explicit_writes
        return (f"{self.number:02d}_{self.name}/*",)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
#
# Order matters — consumers iterate this list to determine pipeline ordering.
# Numbering is contiguous 1..N; each ``Step.number`` matches the ``NN_`` prefix
# on its artifact folder. The folder name itself is auto-derived from
# ``number`` + ``name`` by the ``Step.writes`` property; the constants in
# :mod:`xscore.shared.step_folders` mirror the same pattern for path-builders
# that need a named handle. ``locate_exam_folder`` writes no artifacts of its
# own (``_explicit_writes=()``).

STEPS: tuple[Step, ...] = (
    Step(1,  "parse_grading_instructions",
         title="Interpret prompt", section="Prompt, folder & roster", bootstrap=True),
    Step(2,  "locate_exam_folder", _explicit_writes=(),
         title="Select exam folder", bootstrap=True),
    Step(3,  "read_student_list",
         title="Read student list"),
    Step(4,  "prepare_scans",
         title="Orient and merge scans", section="Scan cleaning"),
    Step(5,  "deskew",
         title="Deskew scanned pages"),
    # Empty-exam analysis (no scan dependency) — pulled up so problems with
    # the empty exam PDF surface early.
    Step(6,  "detect_exam_layout",
         title="Detect empty exam layout", section="Empty-exam analysis",
         phase="empty_exam"),
    Step(7,  "cut_exam_pdf",
         title="Cut empty exam",
         phase="empty_exam"),
    # Cover detection + scan geometry.
    Step(8,  "cover_page_empty_exam",
         title="Detect cover page in empty exam", section="Geometry & validation",
         phase="cover_geometry"),
    Step(9,  "cover_page_scan_first",
         title="Detect cover page in scanned exam",
         phase="cover_geometry"),
    Step(10, "exam_geometry",
         title="Calculate number of scanned exam pages per student",
         phase="cover_geometry"),
    # Two-tier subject detection (filename heuristic → Gemini AI fallback on
    # cover + page 2 of the empty exam). Sets ctx.subject; gates the
    # CODE_FORMATTING prompt section in extract_exam_question_numbers,
    # extract_exam_questions, parse_mark_scheme, ai_marking, extract_student_answers.
    Step(11, "detect_subject",
         title="Detect exam subject (filename → AI fallback)",
         phase="cover_geometry"),
    # Empty-exam page classification (vision LLM, runs once per exam).
    # Builds the catalog used by student_handwriting_check's matcher and
    # detect_cross_page_context's continuation pass.
    Step(12, "classify_empty_exam_pages",
         title="Classify empty-exam pages (cover/instruction/question/blank/writing-space)",
         phase="cover_geometry"),
    # Per-scan-page vision classification — drives student_names and page_order_check.
    Step(13, "student_handwriting_check",
         title="Match each scan page against empty exam (page type + page# + handwriting)",
         phase="cover_geometry"),
    # Cover-anchored student-name detection (consumes student_handwriting_check's covers).
    # Sets ctx.page_assignments — the runner kicks off background pre-rendering
    # immediately after this step (see kick_off_render_bg in pipeline/runner.py).
    Step(14, "student_names",
         title="Detect student names",
         phase="cover_geometry"),
    # Heuristic page-order check (consumes student_handwriting_check's page numbers).
    Step(15, "page_order_check",
         title="Check page order",
         phase="cover_geometry"),
    # Pure data transform: combines handwriting + names into the marking-page
    # register v1. One primary call per non-cover scan page that has handwriting;
    # continuation/figure/parent extras are added by detect_cross_page_context.
    Step(16, "build_marking_register_v1",
         title="Build marking page register",
         phase="cover_geometry"),
    # Empty-exam parse split: question numbers (cheap call) + per-question text (per-page parallel).
    Step(17, "extract_exam_question_numbers",
         title="Extract question numbers from empty exam",
         section="Exam & mark scheme parsing",
         phase="scaffold_phase_b"),
    Step(18, "extract_exam_questions",
         title="Extract questions from empty exam",
         phase="scaffold_phase_b"),
    Step(19, "detect_cross_page_context",  resumable=True,
         title="Detect cross-page context",
         phase="scaffold_phase_b"),
    Step(20, "detect_mark_scheme_graphics",
         title="Detect mark scheme graphics",
         phase="scaffold_phase_b"),
    Step(21, "assign_scheme_questions",
         title="Assign questions to mark scheme pages",
         phase="scaffold_phase_b"),
    Step(22, "parse_mark_scheme",
         title="Parse mark scheme",
         phase="scaffold_phase_b"),
    Step(23, "transcribe_scheme_graphics", resumable=True,
         title="Transcribe mark scheme graphics",
         phase="scaffold_phase_b"),
    Step(24, "create_report",
         title="Build grading scaffold",
         phase="scaffold_phase_b"),
    Step(25, "ai_marking_blueprints",      resumable=True,
         title="Build AI marking blueprints", section="AI marking",
         phase="marking_reports_summary"),
    Step(26, "extract_student_answers",    resumable=True,
         title="Extract student answers (transcribe-only pass)",
         phase="marking_reports_summary"),
    Step(27, "ai_marking",                 resumable=True,
         title="Run AI marking",
         phase="marking_reports_summary"),
    Step(28, "per_student_reports",        resumable=True,
         title="Fuse AI marking output to student reports", section="Reports & PDFs",
         phase="marking_reports_summary"),
    Step(29, "class_stats_curve",          resumable=True,
         title="Compute class statistics + curve",
         phase="marking_reports_summary"),
    Step(30, "per_student_pdfs",           resumable=True,
         title="Generate per-student reports (landscape + portrait + 2UP)",
         phase="marking_reports_summary"),
    Step(31, "class_report",               resumable=True,
         title="Generate class report",
         phase="marking_reports_summary"),
    Step(32, "review_queue",               resumable=True,
         title="Build review queue",
         phase="marking_reports_summary"),
    Step(33, "timing_summary",
         title="Summarise step timings", section="Summary",
         phase="marking_reports_summary"),
    Step(34, "ai_costs",
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
    """Install step bodies into each ``Step`` in ``STEPS``.

    Called once at startup by ``xscore.pipeline.runner.run_pipeline`` after
    ``load_dotenv`` has populated env vars. Idempotent — safe to call again.

    Step modules are imported lazily here so importing :mod:`pipeline_steps`
    for ``STEPS[*].writes`` introspection (e.g. from ``resume.py``) does not
    pull in the entire pipeline at module-load time.

    A missing phase module or a missing step function in a phase module raises
    immediately — every step in ``phase_specs`` must resolve.
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
        mod = importlib.import_module(module_name)
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
        replace(s, fn=fns[s.name]) if s.fn is None and s.name in fns else s
        for s in STEPS
    )

    # Catch the inverse mistake: a Step is in STEPS but its name was forgotten
    # in phase_specs above, so it never got wired. Raises here instead of
    # leaving a None-fn Step that would TypeError inside run_step later.
    unwired = [s.name for s in STEPS if s.fn is None]
    if unwired:
        raise RuntimeError(
            "wire_step_fns: the following STEPS entries have no fn after "
            "wiring (add them to phase_specs in this module): "
            + ", ".join(unwired)
        )
