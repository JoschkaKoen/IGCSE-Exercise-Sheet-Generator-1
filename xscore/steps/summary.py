"""Steps 28–30: timing summary, accuracy evaluation, AI cost report."""

from __future__ import annotations

import json
import time

from eXercise.ai_client import get_run_usage
from xscore.marking.merge_reports import load_student_results_from_reports
from xscore.shared.timing_report import print_step_durations, write_timing_report
from xscore.pipeline.cost_table import print_cost_table
from xscore.shared.cost_report import compute_cost
from xscore.shared.exam_paths import (
    artifact_accuracy_json_path,
    artifact_cost_json_path,
    artifact_cost_md_path,
)
from xscore.shared.load_ground_truth import evaluate_results, load_ground_truth
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.terminal_ui import info_line


def step_28_timing(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
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


def step_29_accuracy(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    if ctx.folder is None:
        info_line("Skipped — no exam folder")
        return
    ground_truth = load_ground_truth(ctx.folder, ctx.scaffold)
    if not ground_truth or not ctx.scaffold:
        info_line("Skipped — no ground truth file")
        return
    student_results = load_student_results_from_reports(ctx.artifact_dir)
    ctx.accuracy_summary = evaluate_results(student_results, ground_truth, ctx.scaffold)
    acc_path = artifact_accuracy_json_path(ctx.artifact_dir)
    acc_path.parent.mkdir(parents=True, exist_ok=True)
    acc_path.write_text(
        json.dumps(ctx.accuracy_summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    info_line(
        f"Accuracy: {ctx.accuracy_summary['overall_correct']}/"
        f"{ctx.accuracy_summary['overall_total']} "
        f"({ctx.accuracy_summary['overall_accuracy_pct']:.1f}%)"
    )


def step_30_costs(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    run_usage = get_run_usage()
    total_cost, breakdown = compute_cost(run_usage)
    payload = {
        "token_usage": breakdown,
        "total_input_tokens": sum(v["input"] for v in run_usage.values()),
        "total_output_tokens": sum(v["output"] for v in run_usage.values()),
        "total_cost_rmb": total_cost,
    }
    cj = artifact_cost_json_path(ctx.artifact_dir)
    cj.parent.mkdir(parents=True, exist_ok=True)
    cj.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    md_lines = [
        "# AI Costs", "",
        "| Model | Input tokens | Output tokens | Cost (RMB) |",
        "|-------|--------------|---------------|------------|",
    ]
    for model, data in breakdown.items():
        md_lines.append(
            f"| {model} | {data['input_tokens']:,} | {data['output_tokens']:,}"
            f" | ¥{data['cost_rmb']:.6f} |"
        )
    md_lines.append(
        f"| **Total** | **{payload['total_input_tokens']:,}**"
        f" | **{payload['total_output_tokens']:,}**"
        f" | **¥{total_cost:.6f}** |"
    )
    artifact_cost_md_path(ctx.artifact_dir).write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8"
    )
    print_cost_table(breakdown, payload["total_input_tokens"],
                     payload["total_output_tokens"], total_cost)
