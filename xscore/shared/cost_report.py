"""Cost computation from accumulated token usage and COST_* env vars."""
from __future__ import annotations

import os
import re


def _model_cost_key(model: str) -> str:
    return re.sub(r"[-.]", "_", model).upper()


def compute_cost(
    usage: dict[str, dict[str, int]],
) -> tuple[float, dict[str, dict]]:
    """Return ``(total_rmb, per_model_breakdown)``.

    *breakdown* maps model name → ``{"input_tokens": N, "output_tokens": N, "cost_rmb": X}``.
    Rates are read from ``COST_<MODEL_KEY>_INPUT`` / ``COST_<MODEL_KEY>_OUTPUT`` env vars
    (RMB per 1 million tokens).  Missing or zero rates produce zero cost for that model.
    """
    breakdown: dict[str, dict] = {}
    total = 0.0
    for model, counts in usage.items():
        key = _model_cost_key(model)
        inp_rate = float(os.environ.get(f"COST_{key}_INPUT", "0") or "0")
        out_rate = float(os.environ.get(f"COST_{key}_OUTPUT", "0") or "0")
        cost = counts["input"] / 1_000_000 * inp_rate + counts["output"] / 1_000_000 * out_rate
        total += cost
        breakdown[model] = {
            "input_tokens": counts["input"],
            "output_tokens": counts["output"],
            "cost_rmb": round(cost, 6),
        }
    return round(total, 6), breakdown
