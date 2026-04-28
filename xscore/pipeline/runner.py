"""Pipeline runner — registry-driven orchestration.

Replaces the manual hand-unrolled ``_run`` loop that lived in xScore.py.
Reads the ``STEPS`` registry, calls ``run_step`` for each step (which handles
skip-if-resumed / stop-after / timing / error capture / run-log emission),
with explicit carve-outs for:

- ``scan_phases`` (steps 4–7, where step 4 is conditional on a duplex match)
- ``scaffold_phase`` (steps 16–23, where a temp split PDF must be cleaned in finally)
- ``kick_off_render_bg`` between steps 12 and 13 (pre-render scan pages so step 25
  can consume them without waiting; needs ``page_assignments`` from step 12)
- The ``ctx.cleaned_pdf and ctx.scaffold`` gate on steps 24–33
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

from xscore.shared.pipeline_ctx import _Ctx, _EarlyExit


def kick_off_render_bg(ctx: _Ctx) -> None:
    """Start parallel page rendering in a background thread right after step 12.

    No-op if cleaned_pdf or page_assignments are not yet set.

    The outer ``ThreadPoolExecutor(max_workers=1)`` exists only to give us a
    ``Future`` handle that step 25 can ``.result()`` on. ``render_pages_b64``
    spawns its own worker pool internally (sized by ``MARKING_WORKERS``).
    """
    if not (ctx.cleaned_pdf and ctx.page_assignments and ctx.artifact_dir):
        return
    from xscore.config import MARKING_DPI
    from xscore.marking.ai_mark import render_pages_b64
    from xscore.shared.terminal_ui import format_duration, info_line, ok_line, warn_line

    instr = getattr(ctx, "instruction", None)
    dpi = getattr(instr, "dpi", None) or MARKING_DPI
    total_pages = sum(len(a.page_numbers) for a in ctx.page_assignments)
    workers = min(
        total_pages,
        int(os.environ.get("MARKING_WORKERS", str(min(os.cpu_count() or 4, 16)))),
    )
    info_line(f"Pre-rendering {total_pages} page(s) in background ({workers} threads, {dpi} DPI) …")
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render_bg")
    t_start = time.perf_counter()
    fut = pool.submit(
        render_pages_b64, ctx.cleaned_pdf, ctx.artifact_dir, dpi, workers,
        instruction=instr,
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
                    f"— will render inline at step 25"
                )
            except Exception:
                pass

    try:
        fut.add_done_callback(_on_done)
    except Exception:
        pass
    ctx.b64_future = fut
    pool.shutdown(wait=False)


def run_pipeline(args: argparse.Namespace, timestamp: str, *, log_path: Path | None = None) -> None:
    """Orchestrate the full 33-step pipeline."""
    from eXercise.ai_client import reset_run_call_stats, reset_run_usage
    from xscore.shared.pipeline_steps import run_step, step_by_number, wire_step_fns
    from xscore.shared.run_log import write_run_manifest
    from xscore.shared.terminal_ui import (
        get_console,
        info_line,
        print_run_footer,
        warn_line,
    )
    from xscore.steps.scan import scan_phases
    from xscore.steps.scaffold import scaffold_phase

    wire_step_fns()
    reset_run_usage()
    reset_run_call_stats()

    ctx = _Ctx(args=args, timestamp=timestamp)
    t0 = time.perf_counter()
    ctx.run_started_at = t0
    started_iso = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")
    early_exit_seen = False
    fatal_exc: BaseException | None = None
    try:
        # Bootstrap: steps 1–2 must always run (mark with bootstrap=True in registry)
        run_step(ctx, step_by_number(1))
        run_step(ctx, step_by_number(2))

        run_step(ctx, step_by_number(3))
        scan_phases(ctx)                                  # 4–7 (4 is conditional)

        if ctx.cleaned_pdf:
            for n in (8, 9, 10, 11, 12):
                run_step(ctx, step_by_number(n))
            kick_off_render_bg(ctx)
            for n in (13, 14, 15):
                run_step(ctx, step_by_number(n))

        scaffold_phase(ctx)                               # 16–23 with finally cleanup

        if ctx.cleaned_pdf and ctx.scaffold:
            for n in range(24, 34):
                run_step(ctx, step_by_number(n))
        elif ctx.cleaned_pdf and not ctx.scaffold:
            warn_line("Marking skipped — scaffold not available (steps 24–33 omitted).")

        ctx.pipeline_completed_ok = True
    except _EarlyExit:
        early_exit_seen = True
        info_line(f"Stopped after step {ctx.stop_after}.")
    except BaseException as exc:
        fatal_exc = exc
        raise
    finally:
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
