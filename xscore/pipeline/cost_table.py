"""Per-model cost-breakdown table for the ai_costs step."""

from __future__ import annotations

from rich import box
from rich.padding import Padding
from rich.table import Table


def fmt_cost_rmb(x: float) -> str:
    if 0 < x < 0.005:
        return "< ¥0.01"
    return f"¥{x:.2f}"


def print_cost_table(
    breakdown: dict,
    total_input: int,
    total_output: int,
    total_thinking: int,
    total_cost: float,
) -> None:
    """Print the per-model cost breakdown as a rich.Table indented to align with
    the surrounding ``  ›  message`` style.

    Rows sort by cost desc; sub-cent values render via :func:`fmt_cost_rmb`.
    ``Output`` already includes thinking tokens; ``Thinking`` is informational.
    """
    from xscore.shared.terminal_ui import get_console, info_line

    if not breakdown:
        info_line(f"API cost: {fmt_cost_rmb(total_cost)}  ·  no model usage recorded")
        return

    table = Table(
        title="API cost",
        title_justify="left",
        title_style="dim",
        box=box.HORIZONTALS,
        header_style="dim",
        show_edge=False,
        pad_edge=False,
    )
    table.add_column("Model", justify="left", style="dim")
    table.add_column("Input", justify="right", style="dim")
    table.add_column("Output", justify="right", style="dim")
    table.add_column("Thinking", justify="right", style="dim")
    table.add_column("Cost", justify="right", style="dim")

    rows = sorted(breakdown.items(), key=lambda kv: kv[1]["cost_rmb"], reverse=True)
    for model, data in rows:
        table.add_row(
            model,
            f"{data['input_tokens']:,}",
            f"{data['output_tokens']:,}",
            f"{data.get('thinking_tokens', 0):,}",
            fmt_cost_rmb(data["cost_rmb"]),
        )

    table.add_section()
    table.add_row(
        "[bold]Total[/]",
        f"[bold]{total_input:,}[/]",
        f"[bold]{total_output:,}[/]",
        f"[bold]{total_thinking:,}[/]",
        f"[bold]{fmt_cost_rmb(total_cost)}[/]",
    )

    # Top padding of 1 visually separates this table from a preceding one
    # (e.g. the per-step cost table printed by print_per_step_cost_table).
    get_console().print(Padding(table, (1, 0, 0, 4)))


def print_per_step_cost_table(per_step_breakdown: dict) -> None:
    """Print a per-step-per-model cost breakdown as a rich.Table.

    *per_step_breakdown* is the dict produced by ``build_per_step_breakdown``:
    ``step_name → {step_number, step_label, models}`` where ``models`` is a
    per-model dict shaped like the breakdown returned by ``compute_cost``,
    extended with ``calls`` (int) and ``avg_duration_s`` (float).

    Steps render in ascending step-number order; within a step, models render
    in cost-descending order. The "Step" column repeats only on the first row
    of each step group to reduce visual noise.
    """
    from xscore.shared.terminal_ui import format_duration, get_console

    if not per_step_breakdown:
        return

    table = Table(
        title="Cost by step",
        title_justify="left",
        title_style="dim",
        box=box.HORIZONTALS,
        header_style="dim",
        show_edge=False,
        pad_edge=False,
    )
    table.add_column("Step", justify="left", style="dim")
    table.add_column("Model", justify="left", style="dim")
    table.add_column("Input", justify="right", style="dim")
    table.add_column("Output", justify="right", style="dim")
    table.add_column("Thinking", justify="right", style="dim")
    table.add_column("Calls", justify="right", style="dim")
    table.add_column("Avg time", justify="right", style="dim")
    table.add_column("Cost", justify="right", style="dim")

    for entry in sorted(per_step_breakdown.values(), key=lambda e: e["step_number"]):
        models = sorted(
            entry["models"].items(), key=lambda kv: kv[1]["cost_rmb"], reverse=True
        )
        first = True
        for model, data in models:
            table.add_row(
                entry["step_label"] if first else "",
                model,
                f"{data['input_tokens']:,}",
                f"{data['output_tokens']:,}",
                f"{data.get('thinking_tokens', 0):,}",
                f"{data['calls']}",
                format_duration(data["avg_duration_s"]),
                fmt_cost_rmb(data["cost_rmb"]),
            )
            first = False

    get_console().print(Padding(table, (0, 0, 0, 4)))
