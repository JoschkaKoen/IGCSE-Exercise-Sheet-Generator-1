"""Terminal cost tables (per-model + per-phase) using rich.

Self-contained — no xscore imports — so eXercise and eXam can print cost
summaries without pulling in xscore's terminal_ui module. xscore imports
these symbols via a re-export shim at :mod:`xscore.pipeline.cost_table`.
"""

from __future__ import annotations

from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.table import Table

_console: Console | None = None


def _get_console() -> Console:
    global _console
    if _console is None:
        _console = Console()
    return _console


def format_duration(seconds: float) -> str:
    """Short duration for CLI (e.g. ``3.1s``, ``1m 5s``)."""
    if seconds < 0:
        seconds = 0.0
    if seconds < 10:
        return f"{seconds:.1f}s"
    if seconds < 60:
        return f"{int(seconds)}s"
    m, s = divmod(int(round(seconds)), 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


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
    """Print the per-model cost breakdown as a rich.Table.

    Rows sort by cost desc; sub-cent values render via :func:`fmt_cost_rmb`.
    ``Output`` already includes thinking tokens; ``Thinking`` is informational.
    """
    if not breakdown:
        print(f"  API cost: {fmt_cost_rmb(total_cost)}  ·  no model usage recorded")
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
    # (e.g. the per-phase cost table).
    _get_console().print(Padding(table, (1, 0, 0, 4)))


def print_per_step_cost_table(per_step_breakdown: dict) -> None:
    """Print a per-phase-per-model cost breakdown as a rich.Table.

    *per_step_breakdown* is the dict produced by ``build_per_phase_breakdown``:
    ``phase_key → {step_number, step_label, models}`` where ``models`` is a
    per-model dict shaped like ``compute_cost`` output, extended with ``calls``
    (int) and ``avg_duration_s`` (float).

    Phases render in ascending ``step_number`` order; within a phase, models
    render in cost-descending order. The "Step" column repeats only on the
    first row of each phase group to reduce visual noise. The column is named
    "Step" for cross-pipeline consistency — xscore uses step numbers, eXercise
    and eXam reuse the same rendering with ``step_number == 0`` and the phase
    name in ``step_label``.
    """
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

    _get_console().print(Padding(table, (0, 0, 0, 4)))
