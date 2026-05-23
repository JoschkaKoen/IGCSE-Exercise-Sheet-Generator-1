"""Per-context cost recorder built on the ai_client observer hook.

A :class:`CostRecorder` is pushed onto the contextvar observer stack via
:func:`collect_run_cost`. Once active, every successful AI call (tracked in
``eXercise.ai_client``) fans out to ``__call__``, which accumulates totals
and (when a phase is active) per-phase counts. ``ThreadPoolExecutor`` callers
that submit AI work must propagate context with ``copy_context().run`` —
see :mod:`eXercise.mcq_explanations` for the canonical example.

eXam supplies an ``on_call`` sink that writes each call to its SQLite
``ai_calls`` table; eXercise leaves ``on_call=None`` and reads the in-memory
totals after the run.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import contextmanager


class CostRecorder:
    """Thread-safe cost aggregator. Drop in as an ai_client observer.

    ``total_usage`` and ``total_calls`` accumulate every call. ``per_phase_*``
    only accumulate while a :meth:`phase` context manager is active — calls
    that fire outside any phase still update totals but are excluded from the
    per-phase breakdown (cleaner display than an "(unphased)" bucket).
    """

    is_null: bool = False

    def __init__(self, *, on_call: Callable[[dict, str, int, int, int, float], None] | None = None):
        self._lock = threading.Lock()
        self._phase_stack: list[dict] = []  # each entry: {"phase": str, **context}
        self.total_usage: dict[str, dict[str, int]] = {}
        self.total_calls: dict[str, dict[str, float]] = {}
        self.per_phase_usage: dict[str, dict[str, dict[str, int]]] = {}
        self.per_phase_calls: dict[str, dict[str, dict[str, float]]] = {}
        self._on_call = on_call

    @contextmanager
    def phase(self, name: str, **context):
        """Open a phase scope; all AI calls during the scope are attributed to *name*.

        Nested ``phase()`` calls update the same recorder's stack — the
        innermost (top of stack) wins for both per-phase attribution and
        ``on_call`` context.
        """
        self._phase_stack.append({"phase": name, **context})
        try:
            yield self
        finally:
            self._phase_stack.pop()

    def __call__(self, model: str, in_t: int, out_t: int, think_t: int, dur: float) -> None:
        with self._lock:
            u = self.total_usage.setdefault(
                model, {"input": 0, "output": 0, "thinking": 0}
            )
            u["input"] += in_t
            u["output"] += out_t
            u["thinking"] += think_t
            c = self.total_calls.setdefault(
                model, {"calls": 0.0, "total_duration_s": 0.0}
            )
            c["calls"] += 1
            c["total_duration_s"] += dur
            top = self._phase_stack[-1] if self._phase_stack else None
            if top is not None:
                pu = self.per_phase_usage.setdefault(top["phase"], {}).setdefault(
                    model, {"input": 0, "output": 0, "thinking": 0}
                )
                pu["input"] += in_t
                pu["output"] += out_t
                pu["thinking"] += think_t
                pc = self.per_phase_calls.setdefault(top["phase"], {}).setdefault(
                    model, {"calls": 0.0, "total_duration_s": 0.0}
                )
                pc["calls"] += 1
                pc["total_duration_s"] += dur
        if self._on_call is not None and top is not None:
            try:
                self._on_call(dict(top), model, in_t, out_t, think_t, dur)
            except Exception:
                pass


class _NullRecorder:
    """Stand-in when no recorder is active. All operations are no-ops."""

    is_null: bool = True
    total_usage: dict = {}
    total_calls: dict = {}
    per_phase_usage: dict = {}
    per_phase_calls: dict = {}

    @contextmanager
    def phase(self, *_args, **_kwargs):
        yield self


@contextmanager
def collect_run_cost(*, on_call: Callable[[dict, str, int, int, int, float], None] | None = None):
    """Push a fresh CostRecorder onto the observer stack for the lifetime of the block."""
    from eXercise.ai_client import pop_call_observer, push_call_observer

    rec = CostRecorder(on_call=on_call)
    token = push_call_observer(rec)
    try:
        yield rec
    finally:
        pop_call_observer(token)


def current_recorder() -> "CostRecorder | _NullRecorder":
    """Return the innermost CostRecorder on the observer stack, or a no-op stub.

    Lets pipeline code call ``current_recorder().phase("X")`` unconditionally —
    callers that aren't inside :func:`collect_run_cost` get a no-op context
    manager and the rest of the pipeline runs unchanged.
    """
    from eXercise.ai_client import _current_observers

    for obs in reversed(_current_observers()):
        if isinstance(obs, CostRecorder):
            return obs
    return _NullRecorder()
