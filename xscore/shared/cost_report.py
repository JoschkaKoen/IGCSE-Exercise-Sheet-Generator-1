"""Cost computation from accumulated token usage and AI API costs.xlsx."""
from __future__ import annotations

from pathlib import Path

_PRICING_FILE = Path(__file__).parents[2] / "AI API costs.xlsx"
_pricing_cache: dict[str, tuple[float, float]] | None = None  # model → (input_rate, output_rate)


def _load_pricing() -> dict[str, tuple[float, float]]:
    """Load pricing from AI API costs.xlsx (cached after first call)."""
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
        inp_col   = next((i for i, h in enumerate(headers) if "input" in h.lower()), None)
        out_col   = next((i for i, h in enumerate(headers) if "output" in h.lower()), None)
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
        pass  # file missing or unreadable → all costs are 0
    _pricing_cache = result
    return result


def compute_cost(
    usage: dict[str, dict[str, int]],
) -> tuple[float, dict[str, dict]]:
    """Return (total_rmb, per_model_breakdown).

    breakdown: model → {"input_tokens": N, "output_tokens": N,
                        "thinking_tokens": N, "cost_rmb": X}
    Rates come from AI API costs.xlsx (RMB per 1M tokens); 0.0 if model not listed.

    ``output_tokens`` is the total billed output (visible + thinking) and is
    multiplied by the output rate. ``thinking_tokens`` is the thinking portion
    of ``output_tokens`` and is informational — not double-counted in the cost.
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
            "input_tokens":    in_tokens,
            "output_tokens":   out_tokens,
            "thinking_tokens": counts.get("thinking", 0),
            "cost_rmb":        round(cost, 6),
        }
    return round(total, 6), breakdown
