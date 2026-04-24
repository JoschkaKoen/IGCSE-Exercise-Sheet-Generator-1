"""Steps 24–25 — Timing summary and AI Costs: write 24_timing_summary/timing.json / timing.md and print tables."""

from __future__ import annotations

import json
from pathlib import Path

_STEP_LABELS: dict[str, str] = {
    "assign_pages_s": "Assign pages (step 8)",
    "blueprints_s":   "Blueprints",
    "marking_s":      "AI marking",
    "reports_s":      "Reports",
    "timing_s":       "Overhead",
}


def print_step_durations(step_durations: dict[str, float], api_calls: list[dict]) -> None:
    """Print the step-duration table to the terminal (no file I/O)."""
    from xscore.shared.terminal_ui import format_duration, info_line
    total = sum(step_durations.values())
    _vis_steps = [
        (_STEP_LABELS.get(k, k.replace("_s", "").replace("_", " ").title()), v)
        for k, v in step_durations.items() if v >= 0.5
    ]
    _lw = max((len(lbl) for lbl, _ in _vis_steps), default=5)
    _lw = max(_lw, len("Total"))
    info_line("Step durations:")
    for _lbl, _val in _vis_steps:
        info_line(f"  {_lbl:<{_lw}}   {format_duration(_val)}")
    info_line(f"  {'Total':<{_lw}}   {format_duration(total)}  ·  {len(api_calls)} API calls")


def write_timing_report(
    artifact_dir: Path,
    step_durations: dict[str, float],
    api_calls: list[dict],
    accuracy_summary: dict | None = None,
    failures: list[dict] | None = None,
    token_usage: dict | None = None,
    total_cost_rmb: float = 0.0,
    print_timing: bool = True,
) -> None:
    """Write timing artifacts and print a summary to the terminal.

    *step_durations* keys are semantic names like ``"marking_s"``, ``"reports_s"`` etc.
    *api_calls* is the list returned by :func:`run_ai_marking`.
    *accuracy_summary* is the optional dict from :func:`evaluate_results`.
    *failures* is the list of page-level marking failures set on ctx by :func:`run_ai_marking`.
    *token_usage* maps model name → ``{"input": N, "output": N}`` (from :func:`get_run_usage`).
    *total_cost_rmb* is the sum of all per-model costs in RMB (from :func:`compute_cost`).
    """
    from xscore.shared.exam_paths import artifact_timing_json_path, artifact_timing_md_path
    from xscore.shared.terminal_ui import info_line, warn_line

    failures = failures or []
    token_usage = token_usage or {}
    total = sum(step_durations.values())
    total_input = sum(v["input"] for v in token_usage.values())
    total_output = sum(v["output"] for v in token_usage.values())

    payload: dict = {
        **{k: round(v, 2) for k, v in step_durations.items()},
        "total_marking_s": round(total, 2),
        "total_api_calls": len(api_calls),
        "total_failures": len(failures),
        "api_calls": api_calls,
    }
    if token_usage:
        from xscore.shared.cost_report import compute_cost
        _, breakdown = compute_cost(token_usage)
        payload["token_usage"] = breakdown
        payload["total_input_tokens"] = total_input
        payload["total_output_tokens"] = total_output
        payload["total_cost_rmb"] = total_cost_rmb
    if failures:
        payload["failures"] = failures
    if accuracy_summary is not None:
        payload["accuracy_summary"] = accuracy_summary

    timing_json = artifact_timing_json_path(artifact_dir)
    timing_json.parent.mkdir(parents=True, exist_ok=True)
    timing_json.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    artifact_timing_md_path(artifact_dir).write_text(
        _timing_to_md(payload), encoding="utf-8"
    )

    if print_timing:
        print_step_durations(step_durations, api_calls)

    # API cost table (column-aligned)
    if token_usage:
        _mw = max((len(m) for m in breakdown), default=5)
        _mw = max(_mw, len("Model"), len("Total"))
        _iw = max(
            max((len(f"{d['input_tokens']:,}") for d in breakdown.values()), default=0),
            len("Input"), len(f"{total_input:,}"),
        )
        _ow = max(
            max((len(f"{d['output_tokens']:,}") for d in breakdown.values()), default=0),
            len("Output"), len(f"{total_output:,}"),
        )
        _cost_strs = [f"¥{d['cost_rmb']:.1f}" for d in breakdown.values()] + [f"¥{total_cost_rmb:.1f}", "Cost"]
        _cw = max(len(s) for s in _cost_strs)
        _sep = "  " + "─" * (_mw + 3 + _iw + 3 + _ow + 3 + _cw)

        from xscore.shared.cost_report import _load_pricing
        _n_prices = len(_load_pricing())
        info_line("")
        info_line("API cost:")
        info_line(f"  {_n_prices} model(s) loaded from AI API costs.xlsx")
        info_line(f"  {'Model':<{_mw}}   {'Input':>{_iw}}   {'Output':>{_ow}}   {'Cost':>{_cw}}")
        info_line(_sep)
        for _model, _data in breakdown.items():
            _cs = f"¥{_data['cost_rmb']:.1f}"
            info_line(
                f"  {_model:<{_mw}}   {_data['input_tokens']:>{_iw},}"
                f"   {_data['output_tokens']:>{_ow},}   {_cs:>{_cw}}"
            )
        info_line(_sep)
        _hint = "" if total_cost_rmb > 0 else "  (prices not found in AI API costs.xlsx)"
        _ts = f"¥{total_cost_rmb:.1f}"
        info_line(
            f"  {'Total':<{_mw}}   {total_input:>{_iw},}"
            f"   {total_output:>{_ow},}   {_ts:>{_cw}}{_hint}"
        )
    if failures:
        warn_line(f"  {len(failures)} page(s) failed marking — see 16_timing.md for details")


def _timing_to_md(payload: dict) -> str:
    step_keys = [
        k for k in payload
        if k.endswith("_s") and k not in ("total_marking_s",) and not k.startswith("total")
    ]
    lines = [
        "# Marking Timing\n",
        "## Step Durations\n",
        "| Step | Duration |",
        "|------|----------|",
    ]
    for k in step_keys:
        label = _STEP_LABELS.get(k, k.replace("_s", "").replace("_", " ").title())
        lines.append(f"| {label} | {payload[k]:.1f}s |")
    lines.append(f"| **Total** | **{payload['total_marking_s']:.1f}s** |")

    if payload.get("token_usage"):
        lines.append("\n## Token Usage & Cost\n")
        lines.append("| Model | Input tokens | Output tokens | Cost (RMB) |")
        lines.append("|-------|-------------|--------------|------------|")
        for model, data in payload["token_usage"].items():
            lines.append(
                f"| {model} | {data['input_tokens']:,} | {data['output_tokens']:,}"
                f" | ¥{data['cost_rmb']:.6f} |"
            )
        lines.append(
            f"| **Total** | **{payload.get('total_input_tokens', 0):,}**"
            f" | **{payload.get('total_output_tokens', 0):,}**"
            f" | **¥{payload.get('total_cost_rmb', 0.0):.6f}** |"
        )

    if payload.get("api_calls"):
        lines.append(f"\n**Total API calls: {payload['total_api_calls']}**\n")
        lines.append("## Per-Call Timings\n")
        lines.append("| Phase | Student | Page | Duration |")
        lines.append("|-------|---------|------|----------|")
        for call in payload["api_calls"]:
            lines.append(
                f"| {call.get('phase', '')} | {call.get('student', '')} "
                f"| {call.get('page', '')} | {call.get('duration_s', '')}s |"
            )

    if payload.get("failures"):
        lines.append(f"\n## Marking Failures ({len(payload['failures'])} page(s))\n")
        lines.append("| Student | Page | Attempts | Error |")
        lines.append("|---------|------|----------|-------|")
        for f in payload["failures"]:
            lines.append(
                f"| {f.get('student', '')} | {f.get('page', '')} "
                f"| {f.get('attempts', '')} | {f.get('error', '')} |"
            )

    if payload.get("accuracy_summary"):
        acc = payload["accuracy_summary"]
        lines.append("\n## Recognition Accuracy vs Ground Truth\n")
        lines.append(
            f"**Overall: {acc['overall_correct']}/{acc['overall_total']}"
            f" ({acc['overall_accuracy_pct']:.1f}%)**\n"
        )
        lines.append("| Student | Correct | Total | Accuracy |")
        lines.append("|---------|---------|-------|----------|")
        for s in acc.get("per_student", []):
            lines.append(
                f"| {s['name']} | {s['correct']} | {s['total']} | {s['accuracy_pct']:.1f}% |"
            )

    return "\n".join(lines) + "\n"
