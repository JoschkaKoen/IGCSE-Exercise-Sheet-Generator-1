"""Compatibility shim — terminal cost tables now live in :mod:`eXercise.cost_table`."""

from eXercise.cost_table import (  # noqa: F401
    fmt_cost_rmb,
    format_duration,
    print_cost_table,
    print_per_step_cost_table,
)
