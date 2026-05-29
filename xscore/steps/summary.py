"""Summary step bodies: timing summary, AI cost report."""

from __future__ import annotations

import time

from eXercise.ai_client import get_run_usage
from eXercise.cost_report import (
    build_per_phase_breakdown,
    compute_cost,
    write_cost_report,
)
from eXercise.cost_table import print_cost_table, print_per_step_cost_table
from xscore.shared.exam_paths import AI_COSTS_DIR
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.pipeline_steps import step_by_name
from xscore.shared.timing_report import _step_label, print_step_durations, write_timing_report


def timing_summary(ctx: _Ctx) -> None:
    if ctx.artifact_dir is None:
        raise RuntimeError('invariant failed: ctx.artifact_dir is not None')
    wall_clock_s = time.perf_counter() - ctx.run_started_at if ctx.run_started_at else None
    print_step_durations(
        ctx.step_timings, ctx.marking_api_calls, wall_clock_s=wall_clock_s
    )
    write_timing_report(
        ctx.artifact_dir,
        ctx.step_timings,
        ctx.marking_api_calls,
        failures=ctx.marking_failures,
        print_timing=False,
    )


def _xscore_phase_order(name: str) -> int:
    s = step_by_name(name)
    return s.number if s else 0


def ai_costs(ctx: _Ctx) -> None:
    if ctx.artifact_dir is None:
        raise RuntimeError('invariant failed: ctx.artifact_dir is not None')
    run_usage = get_run_usage()
    write_cost_report(
        ctx.artifact_dir / AI_COSTS_DIR,
        total_usage=run_usage,
        per_phase_usage=ctx.step_token_usage,
        per_phase_calls=ctx.step_call_stats,
        phase_label="Step",
        phase_order=_xscore_phase_order,
        phase_label_fn=_step_label,
    )
    total_cost, breakdown = compute_cost(run_usage)
    per_step_breakdown = build_per_phase_breakdown(
        ctx.step_token_usage,
        ctx.step_call_stats,
        phase_order=_xscore_phase_order,
        phase_label_fn=_step_label,
    )
    print_per_step_cost_table(per_step_breakdown)
    print_cost_table(
        breakdown,
        sum(v["input"]  for v in run_usage.values()),
        sum(v["output"] for v in run_usage.values()),
        sum(v.get("thinking", 0) for v in run_usage.values()),
        total_cost,
    )
