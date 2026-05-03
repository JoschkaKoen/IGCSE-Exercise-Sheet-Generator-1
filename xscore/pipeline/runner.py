"""Pipeline runner — registry-driven orchestration.

Walks the ``STEPS`` registry once and dispatches each step on its ``phase``
field. ``run_step`` handles the per-step concerns (skip-if-resumed,
stop-after, timing, error capture, run-log emission); the runner here only
applies the runtime *gate* that determines whether a phase's steps run at
all in this invocation.

Carve-outs:

- The bootstrap steps (``parse_grading_instructions``, ``locate_exam_folder``)
  and ``read_student_list`` are dispatched explicitly so the resume bootstrap
  in ``locate_exam_folder`` runs before any phase-gated step.
- Scan-cleaning (``prepare_scans`` … ``deskew``) is dispatched via
  ``scan_phases`` because ``prepare_scans`` is conditional on a duplex match.
- ``scaffold_setup`` populates the empty-exam state shared by phases
  ``empty_exam`` and ``scaffold_phase_b``; ``scaffold_cleanup`` runs in the
  ``finally`` block to drop the temp split PDF.
- ``kick_off_render_bg`` fires immediately after ``student_names`` (the step
  that populates ``ctx.page_assignments``) so background pre-rendering can
  proceed in parallel with the rest of the geometry / scaffold work.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from xscore.shared.pipeline_ctx import _Ctx, _EarlyExit


def kick_off_render_bg(ctx: _Ctx) -> None:
    """Start parallel page rendering in a background thread right after ``student_names``.

    No-op if cleaned_pdf or page_assignments are not yet set.

    The outer ``ThreadPoolExecutor(max_workers=1)`` exists only to give us a
    ``Future`` handle that ``ai_marking_blueprints`` can ``.result()`` on.
    ``render_pages_b64`` spawns its own worker pool internally (sized by
    ``MARKING_WORKERS``).
    """
    if not (ctx.cleaned_pdf and ctx.page_assignments and ctx.artifact_dir):
        return
    from xscore.config import MARKING_DPI
    from xscore.marking.ai_mark import render_pages_b64
    from xscore.shared.terminal_ui import format_duration, info_line, ok_line, warn_line

    instr = getattr(ctx, "instruction", None)
    cli_filter = getattr(ctx, "student_filter", None)
    limit_students = getattr(ctx, "limit_students", None)
    dpi = getattr(instr, "dpi", None) or MARKING_DPI

    filtered_assignments = list(ctx.page_assignments)
    if instr is not None:
        sf = instr.student_filter
        if sf.mode == "specific" and sf.names:
            filtered_assignments = [a for a in filtered_assignments if a.student_name in sf.names]
        elif sf.mode == "first_n" and sf.n:
            filtered_assignments = filtered_assignments[: sf.n]
    if cli_filter:
        wanted = {n.strip().lower() for n in cli_filter}
        filtered_assignments = [
            a for a in filtered_assignments
            if (a.student_name or "").strip().lower() in wanted
        ]
    if limit_students:
        filtered_assignments = filtered_assignments[:limit_students]
    total_pages = sum(len(a.page_numbers) for a in filtered_assignments)
    if total_pages == 0:
        return
    _default_workers = min(os.cpu_count() or 4, 16)
    try:
        _env_workers = int(os.environ.get("MARKING_WORKERS", str(_default_workers)))
    except ValueError:
        from xscore.shared.terminal_ui import warn_line
        warn_line(
            f"MARKING_WORKERS={os.environ.get('MARKING_WORKERS')!r} is not an "
            f"integer — falling back to default {_default_workers}."
        )
        _env_workers = _default_workers
    workers = min(total_pages, _env_workers)
    info_line(f"Pre-rendering {total_pages} page(s) in background ({workers} threads, {dpi} DPI) …")
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render_bg")
    t_start = time.perf_counter()
    fut = pool.submit(
        render_pages_b64, ctx.cleaned_pdf, ctx.artifact_dir, dpi, workers,
        instruction=instr,
        cli_filter=cli_filter,
        limit_students=limit_students,
    )

    def _on_done(f) -> None:
        elapsed = time.perf_counter() - t_start
        try:
            cache = f.result()
            ok_line(f"Pre-rendering done  ·  {len(cache)} page(s) ready  ·  {format_duration(elapsed)}")
        except Exception as exc:  # noqa: BLE001
            try:
                warn_line(
                    f"Background pre-rendering failed ({exc}) after {format_duration(elapsed)} "
                    f"— will render inline at the ai_marking_blueprints step"
                )
            except Exception:
                pass

    try:
        fut.add_done_callback(_on_done)
    except Exception:
        pass
    ctx.b64_future = fut
    pool.shutdown(wait=False)


def run_pipeline(
    args: argparse.Namespace,
    timestamp: str,
    *,
    log_path: Path | None = None,
    on_step_event: "Callable[[dict], None] | None" = None,
) -> None:
    """Orchestrate the full pipeline (see ``xscore.shared.pipeline_steps.STEPS``).

    *on_step_event*, when provided, receives one event dict per step transition
    ({step_number, step_name, status, duration_s, artifact_dir, error}). Used by
    in-process consumers (e.g. the FastAPI web grade page) to track per-step
    state without parsing stdout. Observer faults are swallowed inside
    ``run_step``; a misbehaving consumer cannot crash the pipeline.
    """
    from eXercise.ai_client import reset_run_call_stats, reset_run_usage
    from xscore.shared.pipeline_steps import (
        run_step,
        step_by_number,
        wire_step_fns,
    )
    from xscore.shared.run_log import write_run_manifest
    from xscore.shared.terminal_ui import (
        get_console,
        info_line,
        print_run_footer,
        warn_line,
    )
    from xscore.steps.scan import scan_phases
    from xscore.steps.scaffold import scaffold_setup, scaffold_cleanup

    wire_step_fns()
    # Import STEPS *after* wire_step_fns rebinds the module global so we walk
    # the wired-up tuple (functions installed) rather than the _unmigrated stubs.
    from xscore.shared.pipeline_steps import STEPS

    reset_run_usage()
    reset_run_call_stats()

    ctx = _Ctx(args=args, timestamp=timestamp)
    ctx.on_step_event = on_step_event
    t0 = time.perf_counter()
    ctx.run_started_at = t0
    started_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
    early_exit_seen = False
    fatal_exc: BaseException | None = None
    scaffold_inited = False
    try:
        # Bootstrap: parse + locate-folder must run before anything phase-gated
        # so resume can populate ctx (instruction, folder, artifact_dir, and
        # — when --from-step is set — cleaned_pdf, scaffold, page_assignments).
        run_step(ctx, step_by_number(1))
        run_step(ctx, step_by_number(2))

        # Roster + scan-cleaning. ``scan_phases`` is a helper because
        # ``prepare_scans`` is conditional on duplex match detection.
        run_step(ctx, step_by_number(3))
        scan_phases(ctx)

        # Initialize scaffold state up-front so the empty-exam blank-detection
        # step can read the cut PDF later, and the scaffold detect/fill phases
        # can finish without re-init.
        scaffold_inited = scaffold_setup(ctx)

        # Walk every remaining step in registry order, dispatching on phase.
        # Each phase has a single gate (whether the step's prerequisites are
        # available in ctx); per-step concerns (skip-on-from-step, stop-after,
        # timing, error capture) are handled inside ``run_step``.
        for step in STEPS:
            if step.phase is None:
                continue  # bootstrap / roster / scan-cleaning — already dispatched

            if step.phase == "empty_exam":
                if not scaffold_inited:
                    continue
            elif step.phase == "cover_geometry":
                if not ctx.cleaned_pdf:
                    continue
            elif step.phase == "scaffold_phase_b":
                if not scaffold_inited:
                    continue
            elif step.phase == "marking_reports_summary":
                if not (ctx.cleaned_pdf and ctx.scaffold):
                    continue
            else:
                # Unknown phase — fail loudly so a typo on a registry entry
                # surfaces as an error instead of silently skipping the step.
                raise ValueError(
                    f"step {step.number} ({step.name!r}) has unknown phase {step.phase!r}"
                )

            run_step(ctx, step)

            # Hook: ``student_names`` populates ``ctx.page_assignments``;
            # spin up background pre-rendering as soon as it's available so
            # the rest of geometry + scaffold can overlap with rendering.
            if step.name == "student_names":
                kick_off_render_bg(ctx)

        if ctx.cleaned_pdf and not ctx.scaffold:
            warn_line("Marking skipped — scaffold not available; AI-marking and reporting steps omitted.")

        ctx.pipeline_completed_ok = True
    except _EarlyExit:
        early_exit_seen = True
        info_line(f"Stopped after step {ctx.stop_after}.")
    except BaseException as exc:
        fatal_exc = exc
        raise
    finally:
        if scaffold_inited:
            scaffold_cleanup(ctx)
        elapsed_total = time.perf_counter() - t0
        if ctx.pipeline_completed_ok:
            run_status = "ok"
        elif early_exit_seen:
            run_status = "early_exit"
        elif fatal_exc is not None:
            run_status = "error"
        else:
            run_status = "incomplete"
        print_run_footer(
            cleaned_pdf=ctx.cleaned_pdf,
            elapsed=elapsed_total,
            status=run_status,
        )
        try:
            write_run_manifest(
                ctx,
                status=run_status,
                timestamp_started=started_iso,
                duration_s=elapsed_total,
            )
        except Exception:
            pass  # never let manifest writing kill the pipeline
        get_console().print()
        sys.stdout.flush()
        try:
            if log_path and ctx.artifact_dir and log_path.exists():
                shutil.copy2(log_path, ctx.artifact_dir / "pipeline.log")
        except Exception:
            pass
