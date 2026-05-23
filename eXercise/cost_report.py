"""Cost computation + report writer.

Pricing is loaded once from ``AI API costs.xlsx`` at the repo root (cached after
the first call). All public functions are pure (no observer / pipeline
coupling) so they can be reused by xscore, eXercise, and eXam.

``output_tokens`` is the total billed output (visible + thinking) and is
multiplied by the output rate. ``thinking_tokens`` is the thinking portion of
``output_tokens`` and is informational — not double-counted in the cost.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

_PRICING_FILE = Path(__file__).parent.parent / "AI API costs.xlsx"
_pricing_cache: dict[str, tuple[float, float]] | None = None  # model → (input_rate, output_rate)


def _load_pricing() -> dict[str, tuple[float, float]]:
    """Load pricing from AI API costs.xlsx (cached after first call).

    Returns a dict ``model → (input_rate_per_1m, output_rate_per_1m)``. Missing
    file or unreadable rows fall back to an empty dict — callers default the
    rate to ``0.0`` so cost stays at zero rather than crashing.
    """
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache
    result: dict[str, tuple[float, float]] = {}
    try:
        import openpyxl

        wb = openpyxl.load_workbook(_PRICING_FILE, read_only=True, data_only=True)
        ws = wb.active
        rows = iter(ws.rows)
        headers = [str(c.value).strip() if c.value else "" for c in next(rows)]
        model_col = next((i for i, h in enumerate(headers) if "model" in h.lower()), None)
        inp_col = next((i for i, h in enumerate(headers) if "input" in h.lower()), None)
        out_col = next((i for i, h in enumerate(headers) if "output" in h.lower()), None)
        if None not in (model_col, inp_col, out_col):
            for row in rows:
                model = str(row[model_col].value or "").strip()
                if not model:
                    continue
                try:
                    inp = float(row[inp_col].value or 0)
                    out = float(row[out_col].value or 0)
                except (TypeError, ValueError):
                    inp, out = 0.0, 0.0
                result[model] = (inp, out)
        wb.close()
    except Exception:
        pass
    _pricing_cache = result
    return result


def compute_cost(
    usage: dict[str, dict[str, int]],
) -> tuple[float, dict[str, dict]]:
    """Return ``(total_rmb, per_model_breakdown)``.

    breakdown: ``model → {"input_tokens": N, "output_tokens": N,
    "thinking_tokens": N, "cost_rmb": X}``. Rates come from
    ``AI API costs.xlsx`` (RMB per 1M tokens); 0.0 if model not listed.
    """
    pricing = _load_pricing()
    breakdown: dict[str, dict] = {}
    total = 0.0
    for model, counts in usage.items():
        inp_rate, out_rate = pricing.get(model, (0.0, 0.0))
        in_tokens = counts.get("input", 0)
        out_tokens = counts.get("output", 0)
        cost = in_tokens / 1_000_000 * inp_rate + out_tokens / 1_000_000 * out_rate
        total += cost
        breakdown[model] = {
            "input_tokens": in_tokens,
            "output_tokens": out_tokens,
            "thinking_tokens": counts.get("thinking", 0),
            "cost_rmb": round(cost, 6),
        }
    return round(total, 6), breakdown


def compute_one(model: str, input_tokens: int, output_tokens: int) -> float:
    """RMB cost for a single call (used by eXam's DB sink at insert time)."""
    inp_rate, out_rate = _load_pricing().get(model, (0.0, 0.0))
    return round(input_tokens / 1_000_000 * inp_rate + output_tokens / 1_000_000 * out_rate, 6)


def build_per_phase_breakdown(
    per_phase_usage: dict[str, dict[str, dict[str, int]]],
    per_phase_calls: dict[str, dict[str, dict[str, float]]],
    *,
    phase_label: str = "Phase",
    phase_order: Callable[[str], int] | None = None,
    phase_label_fn: Callable[[str], str] | None = None,
) -> dict[str, dict]:
    """Convert per-phase usage + call stats into a display-ready breakdown.

    Returns ``phase_key → {step_number, step_label, models}`` where ``models``
    is shaped like ``compute_cost``'s per-model dict, extended with ``calls``
    (int) and ``avg_duration_s`` (float). Phases with no usage are omitted.

    ``step_number`` is supplied by ``phase_order`` (e.g. xscore's step number)
    or falls back to ``0`` for callers without a natural ordering.
    ``phase_label_fn`` lets callers map the bare phase key to a human-readable
    label (xscore turns ``ai_marking`` → ``"AI marking"``); defaults to
    identity. The ``step_*`` field names are kept identical to xscore's prior
    schema so ``print_per_step_cost_table`` consumes both shapes unchanged.
    """
    out: dict[str, dict] = {}
    for phase_name, per_model in per_phase_usage.items():
        if not per_model:
            continue
        _, models = compute_cost(per_model)
        per_model_calls = per_phase_calls.get(phase_name, {})
        for model, data in models.items():
            cs = per_model_calls.get(model, {"calls": 0.0, "total_duration_s": 0.0})
            data["calls"] = int(cs["calls"])
            data["avg_duration_s"] = (
                cs["total_duration_s"] / cs["calls"] if cs["calls"] else 0.0
            )
        out[phase_name] = {
            "step_number": phase_order(phase_name) if phase_order else 0,
            "step_label": phase_label_fn(phase_name) if phase_label_fn else phase_name,
            "models": models,
        }
    return out


def write_cost_report(
    out_dir: Path,
    *,
    total_usage: dict[str, dict[str, int]],
    per_phase_usage: dict[str, dict[str, dict[str, int]]],
    per_phase_calls: dict[str, dict[str, dict[str, float]]],
    phase_label: str = "Phase",
    phase_order: Callable[[str], int] | None = None,
    phase_label_fn: Callable[[str], str] | None = None,
) -> Path:
    """Write ``cost.json`` + ``cost.md`` into *out_dir* and return the JSON path.

    Schema matches xscore's prior output so downstream consumers (web
    UI download buttons, manual review) see the same shape regardless of
    which pipeline produced it. The ``by_step`` key is kept (rather than
    ``by_phase``) for cross-pipeline compatibility.
    """
    from eXercise.cost_table import format_duration

    out_dir.mkdir(parents=True, exist_ok=True)
    total_cost, breakdown = compute_cost(total_usage)
    per_phase_breakdown = build_per_phase_breakdown(
        per_phase_usage, per_phase_calls,
        phase_label=phase_label,
        phase_order=phase_order,
        phase_label_fn=phase_label_fn,
    )
    payload = {
        "token_usage": breakdown,
        "total_input_tokens": sum(v["input"] for v in total_usage.values()),
        "total_output_tokens": sum(v["output"] for v in total_usage.values()),
        "total_thinking_tokens": sum(v.get("thinking", 0) for v in total_usage.values()),
        "total_cost_rmb": total_cost,
        "by_step": per_phase_breakdown,
    }
    cj = out_dir / "cost.json"
    cj.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    md_lines = [
        "# AI Costs", "",
        "| Model | Input tokens | Output tokens | Thinking tokens | Cost (RMB) |",
        "|-------|--------------|---------------|-----------------|------------|",
    ]
    for model, data in breakdown.items():
        md_lines.append(
            f"| {model} | {data['input_tokens']:,} | {data['output_tokens']:,}"
            f" | {data['thinking_tokens']:,}"
            f" | ¥{data['cost_rmb']:.6f} |"
        )
    md_lines.append(
        f"| **Total** | **{payload['total_input_tokens']:,}**"
        f" | **{payload['total_output_tokens']:,}**"
        f" | **{payload['total_thinking_tokens']:,}**"
        f" | **¥{total_cost:.6f}** |"
    )
    md_lines.append("")
    md_lines.append(
        "_Output tokens already include thinking tokens; the Thinking column is informational._"
    )
    if per_phase_breakdown:
        md_lines.append("")
        md_lines.append(f"## Cost by {phase_label.lower()}")
        md_lines.append("")
        md_lines.append(
            f"| {phase_label} | Model | Input tokens | Output tokens | Thinking tokens"
            " | Calls | Avg time | Cost (RMB) |"
        )
        md_lines.append(
            "|------|-------|--------------|---------------|-----------------"
            "|-------|----------|------------|"
        )
        for entry in sorted(per_phase_breakdown.values(), key=lambda e: e["step_number"]):
            sorted_models = sorted(
                entry["models"].items(),
                key=lambda kv: kv[1]["cost_rmb"],
                reverse=True,
            )
            first = True
            for model, data in sorted_models:
                phase_cell = entry["step_label"] if first else ""
                md_lines.append(
                    f"| {phase_cell} | {model} | {data['input_tokens']:,}"
                    f" | {data['output_tokens']:,}"
                    f" | {data['thinking_tokens']:,}"
                    f" | {data['calls']}"
                    f" | {format_duration(data['avg_duration_s'])}"
                    f" | ¥{data['cost_rmb']:.6f} |"
                )
                first = False
    (out_dir / "cost.md").write_text("\n".join(md_lines) + "\n", encoding="utf-8")
    return cj
