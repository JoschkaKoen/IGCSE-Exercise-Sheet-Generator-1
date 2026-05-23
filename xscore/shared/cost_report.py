"""Compatibility shim — cost-report helpers now live in :mod:`eXercise.cost_report`.

The pure functions (``_load_pricing``, ``compute_cost``) were promoted into
eXercise so eXam and eXercise can use them without depending on xscore. This
shim re-exports them so existing ``from xscore.shared.cost_report import …``
call sites keep working.
"""

from eXercise.cost_report import (  # noqa: F401
    _load_pricing,
    build_per_phase_breakdown,
    compute_cost,
    compute_one,
    write_cost_report,
)
