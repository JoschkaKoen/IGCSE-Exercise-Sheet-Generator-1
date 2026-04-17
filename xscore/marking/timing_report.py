"""Step 14 — Timing summary: write 14_timing.json / 14_timing.md and print table."""

from __future__ import annotations

import json
from pathlib import Path

_STEP_LABELS: dict[str, str] = {
    "step_10_s": "Name detection (10)",
    "step_11_s": "Blueprints (11)",
    "step_12_s": "AI marking (12)",
    "step_13_s": "Reports (13)",
    "step_14_s": "Timing (14)",
}


def write_timing_report(
    artifact_dir: Path,
    step_durations: dict[str, float],
    api_calls: list[dict],
    accuracy_summary: dict | None = None,
) -> None:
    """Write timing artifacts and print a summary to the terminal.

    *step_durations* keys are like ``"step_10_s"``, ``"step_11_s"`` etc.
    *api_calls* is the list returned by :func:`run_ai_marking`.
    *accuracy_summary* is the optional dict from :func:`evaluate_results`.
    """
    from xscore.shared.exam_paths import artifact_timing_json_path, artifact_timing_md_path
    from xscore.shared.terminal_ui import format_duration, info_line

    total = sum(step_durations.values())
    payload: dict = {
        **{k: round(v, 2) for k, v in step_durations.items()},
        "total_marking_s": round(total, 2),
        "total_api_calls": len(api_calls),
        "api_calls": api_calls,
    }
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

    # Terminal summary
    info_line("Marking timing:")
    for key, val in step_durations.items():
        label = _STEP_LABELS.get(key, key.replace("_s", "").replace("_", " ").title())
        info_line(f"  {label}: {format_duration(val)}")
    info_line(f"  Total: {format_duration(total)}  ·  {len(api_calls)} API calls")


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
