"""Per-model cost-breakdown table for step 30 (AI costs)."""

from __future__ import annotations


def fmt_cost_rmb(x: float) -> str:
    if 0 < x < 0.005:
        return "< ¥0.01"
    return f"¥{x:.2f}"


def print_cost_table(
    breakdown: dict,
    total_input: int,
    total_output: int,
    total_cost: float,
) -> None:
    """Print the per-model cost breakdown as a column-aligned info_line block.

    Each row is emitted via info_line so the existing two-space + '›' + two-space
    indent is preserved (matches the rest of the run output). Rows sort by cost
    desc; sub-cent values render via :func:`fmt_cost_rmb`.
    """
    from xscore.shared.terminal_ui import info_line

    if not breakdown:
        info_line(f"API cost: {fmt_cost_rmb(total_cost)}  ·  no model usage recorded")
        return

    rows = sorted(breakdown.items(), key=lambda kv: kv[1]["cost_rmb"], reverse=True)
    cost_strs = [fmt_cost_rmb(d["cost_rmb"]) for _, d in rows] + [fmt_cost_rmb(total_cost)]
    in_strs   = [f"{d['input_tokens']:,}"  for _, d in rows] + [f"{total_input:,}"]
    out_strs  = [f"{d['output_tokens']:,}" for _, d in rows] + [f"{total_output:,}"]

    mw = max((len(m) for m, _ in rows), default=5)
    mw = max(mw, len("Model"), len("Total"))
    iw = max(max((len(s) for s in in_strs),  default=0), len("Input"))
    ow = max(max((len(s) for s in out_strs), default=0), len("Output"))
    cw = max(max((len(s) for s in cost_strs), default=0), len("Cost"))
    sep = "─" * (mw + 3 + iw + 3 + ow + 3 + cw)

    info_line("API cost:")
    info_line(f"  {'Model':<{mw}}   {'Input':>{iw}}   {'Output':>{ow}}   {'Cost':>{cw}}")
    info_line(f"  {sep}")
    for (model, data), cs, ins, outs in zip(rows, cost_strs, in_strs, out_strs):
        info_line(f"  {model:<{mw}}   {ins:>{iw}}   {outs:>{ow}}   {cs:>{cw}}")
    info_line(f"  {sep}")
    info_line(
        f"  {'Total':<{mw}}   {in_strs[-1]:>{iw}}   {out_strs[-1]:>{ow}}   {cost_strs[-1]:>{cw}}"
    )


def print_per_step_cost_table(per_step_breakdown: dict) -> None:
    """Print a per-step-per-model cost breakdown.

    *per_step_breakdown* is the dict produced by ``build_per_step_breakdown``:
    ``step_name → {step_number, step_label, models}`` where ``models`` is a
    per-model dict shaped like the breakdown returned by ``compute_cost``.

    Steps render in ascending step-number order; within a step, models render
    in cost-descending order. The "Step" column repeats only on the first row
    of each step group to reduce visual noise.
    """
    from xscore.shared.terminal_ui import info_line

    if not per_step_breakdown:
        return

    # Flatten into (step_label, model, ins, outs, cost_str, cost_rmb) rows.
    rows: list[tuple[str, str, str, str, str]] = []
    for entry in sorted(per_step_breakdown.values(), key=lambda e: e["step_number"]):
        models = sorted(
            entry["models"].items(), key=lambda kv: kv[1]["cost_rmb"], reverse=True
        )
        first = True
        for model, data in models:
            rows.append((
                entry["step_label"] if first else "",
                model,
                f"{data['input_tokens']:,}",
                f"{data['output_tokens']:,}",
                fmt_cost_rmb(data["cost_rmb"]),
            ))
            first = False

    sw = max((len(r[0]) for r in rows), default=0)
    sw = max(sw, len("Step"))
    mw = max((len(r[1]) for r in rows), default=0)
    mw = max(mw, len("Model"))
    iw = max((len(r[2]) for r in rows), default=0)
    iw = max(iw, len("Input"))
    ow = max((len(r[3]) for r in rows), default=0)
    ow = max(ow, len("Output"))
    cw = max((len(r[4]) for r in rows), default=0)
    cw = max(cw, len("Cost"))
    sep = "─" * (sw + 3 + mw + 3 + iw + 3 + ow + 3 + cw)

    info_line("")
    info_line("Cost by step:")
    info_line(
        f"  {'Step':<{sw}}   {'Model':<{mw}}   {'Input':>{iw}}"
        f"   {'Output':>{ow}}   {'Cost':>{cw}}"
    )
    info_line(f"  {sep}")
    for step_label, model, ins, outs, cs in rows:
        info_line(
            f"  {step_label:<{sw}}   {model:<{mw}}   {ins:>{iw}}"
            f"   {outs:>{ow}}   {cs:>{cw}}"
        )
