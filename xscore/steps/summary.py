"""Steps 29–31: timing summary, accuracy evaluation, AI cost report."""

from __future__ import annotations

import json
import time

from eXercise.ai_client import get_run_usage
from xscore.marking.merge_reports import load_student_results_from_reports
from xscore.shared.timing_report import _step_label, print_step_durations, write_timing_report
from xscore.pipeline.cost_table import print_cost_table, print_per_step_cost_table
from xscore.shared.cost_report import compute_cost
from xscore.shared.exam_paths import (
    artifact_accuracy_json_path,
    artifact_cost_json_path,
    artifact_cost_md_path,
)
from xscore.shared.load_ground_truth import evaluate_results, load_ground_truth
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.pipeline_steps import step_by_name
from xscore.shared.terminal_ui import format_duration, info_line


def step_29_timing(ctx: _Ctx) -> None:
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


def step_30_accuracy(ctx: _Ctx) -> None:
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


def _build_per_step_breakdown(
    step_token_usage: dict[str, dict[str, dict[str, int]]],
    step_call_stats: dict[str, dict[str, dict[str, float]]],
) -> dict[str, dict]:
    """Convert ctx.step_token_usage + ctx.step_call_stats into a per-step cost breakdown.

    Returns ``step_name → {step_number, step_label, models}`` where ``models``
    has the same shape as the per-model dict from :func:`compute_cost`, with
    ``calls`` (int) and ``avg_duration_s`` (float) attached to each per-model
    entry. Steps with no usage are omitted.
    """
    out: dict[str, dict] = {}
    for step_name, per_model in step_token_usage.items():
        if not per_model:
            continue
        _, models = compute_cost(per_model)
        per_model_calls = step_call_stats.get(step_name, {})
        for model, data in models.items():
            cs = per_model_calls.get(model, {"calls": 0.0, "total_duration_s": 0.0})
            data["calls"] = int(cs["calls"])
            data["avg_duration_s"] = (
                cs["total_duration_s"] / cs["calls"] if cs["calls"] else 0.0
            )
        s = step_by_name(step_name)
        out[step_name] = {
            "step_number": s.number if s else 0,
            "step_label":  _step_label(step_name),
            "models":      models,
        }
    return out


def step_31_costs(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    run_usage = get_run_usage()
    total_cost, breakdown = compute_cost(run_usage)
    per_step_breakdown = _build_per_step_breakdown(ctx.step_token_usage, ctx.step_call_stats)
    payload = {
        "token_usage": breakdown,
        "total_input_tokens": sum(v["input"] for v in run_usage.values()),
        "total_output_tokens": sum(v["output"] for v in run_usage.values()),
        "total_cost_rmb": total_cost,
        "by_step": per_step_breakdown,
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
    if per_step_breakdown:
        md_lines.append("")
        md_lines.append("## Cost by step")
        md_lines.append("")
        md_lines.append("| Step | Model | Input tokens | Output tokens | Calls | Avg time | Cost (RMB) |")
        md_lines.append("|------|-------|--------------|---------------|-------|----------|------------|")
        for entry in sorted(per_step_breakdown.values(), key=lambda e: e["step_number"]):
            sorted_models = sorted(
                entry["models"].items(),
                key=lambda kv: kv[1]["cost_rmb"],
                reverse=True,
            )
            first = True
            for model, data in sorted_models:
                step_cell = entry["step_label"] if first else ""
                md_lines.append(
                    f"| {step_cell} | {model} | {data['input_tokens']:,}"
                    f" | {data['output_tokens']:,}"
                    f" | {data['calls']}"
                    f" | {format_duration(data['avg_duration_s'])}"
                    f" | ¥{data['cost_rmb']:.6f} |"
                )
                first = False
    artifact_cost_md_path(ctx.artifact_dir).write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8"
    )
    print_per_step_cost_table(per_step_breakdown)
    print_cost_table(breakdown, payload["total_input_tokens"],
                     payload["total_output_tokens"], total_cost)
