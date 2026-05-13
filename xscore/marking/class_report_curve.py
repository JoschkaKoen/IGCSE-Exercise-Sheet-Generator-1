"""Grade-curving math + curve-related configuration knobs.

Four pure-ish helpers extracted from ``class_report``: three resolve the
curve target / visibility config from the natural-language instruction or
env var, and :func:`_apply_grade_curve` is the closed-form solver that
mutates per-student summaries with the curved percentage and returns the
applied offset.
"""

from __future__ import annotations

import os
from typing import Any

from xscore.shared.terminal_ui import warn_line


def _grade_curve_target() -> int:
    """Read GRADE_CURVE_TARGET (default 80). Used as the env-var fallback
    when the natural-language prompt doesn't override the target."""
    raw = os.environ.get("GRADE_CURVE_TARGET", "80")
    try:
        return int(raw)
    except ValueError:
        warn_line(f"Invalid GRADE_CURVE_TARGET={raw!r} — using default 80")
        return 80


def _effective_curve_target(ctx: Any) -> int:
    """Resolve the curve target for *ctx*.

    Priority: ``ctx.instruction.curved_grade_override`` (if int) → env var
    ``GRADE_CURVE_TARGET`` (default 80).
    """
    instr = getattr(ctx, "instruction", None)
    if instr is not None:
        override = getattr(instr, "curved_grade_override", None)
        if override is not None:
            return int(override)
    return _grade_curve_target()


_TRUE_STRS  = {"true",  "1", "yes", "on"}
_FALSE_STRS = {"false", "0", "no",  "off"}


def _curved_grade_visible(ctx: Any) -> bool:
    """Resolve whether per-student PDFs include the curved % in their header.

    Priority: ``ctx.instruction.curved_grade_visible`` (if bool) → env var
    ``CURVED_GRADE_VISIBLE`` (default true). Unrecognised env values warn
    and fall back to True.
    """
    instr = getattr(ctx, "instruction", None)
    if instr is not None:
        override = getattr(instr, "curved_grade_visible", None)
        if override is not None:
            return bool(override)
    raw = os.environ.get("CURVED_GRADE_VISIBLE", "true").strip().lower()
    if raw in _TRUE_STRS:
        return True
    if raw in _FALSE_STRS:
        return False
    warn_line(f"Invalid CURVED_GRADE_VISIBLE={raw!r} — using default true")
    return True

def _apply_grade_curve(student_summaries: list[dict], target: int) -> int:
    """Mutate summaries in place; return the offset actually applied (post-clip).

    Solves for the offset *x* such that ``mean(min(100, raw + x)) == target``
    so the curved class mean lands on the target even when top students would
    overflow the 100% cap. The naive ``target − raw_mean`` is computed pre-clip;
    when any student's curved score would exceed 100, the per-student
    ``min(100, …)`` truncates the excess and the actual class mean falls below
    target by the lost amount divided by *n*.

    The function ``mean(min(100, raw + x))`` is monotone non-decreasing in *x*,
    so a closed-form iteration over candidate cap-counts ``k ∈ {0..n}`` gives
    an exact answer in at most *n* iterations. For each *k* (top-k students
    capped), with *S* = sum of the *n−k* lowest raws, solve
    ``x = (n·target − 100k − S) / (n−k)``. Accept the *k* where the boundary
    raws are consistent: the (k+1)-th-highest does not exceed ``100−x``, the
    k-th-highest does (when both exist).

    The returned offset is rounded to int for display in ``class_stats.json``;
    the per-student ``curved_pct`` uses the unrounded value for accuracy.
    """
    raws = [s["percentage"] for s in student_summaries if s["percentage"] is not None]
    if not raws:
        for s in student_summaries:
            s["curved_pct"] = None
        return 0
    n = len(raws)
    sorted_desc = sorted(raws, reverse=True)
    offset = target - sum(raws) / n  # k=0 baseline (matches pre-fix behaviour)
    for k in range(n + 1):
        n_unc = n - k
        if n_unc == 0:
            continue
        sum_unc = sum(sorted_desc[k:])
        x = (n * target - 100 * k - sum_unc) / n_unc
        top_unc = sorted_desc[k] if k < n else None
        bot_cap = sorted_desc[k - 1] if k > 0 else None
        if (top_unc is None or top_unc + x <= 100 + 1e-9) and \
           (bot_cap is None or bot_cap + x >= 100 - 1e-9):
            offset = x
            break
    offset = max(0.0, offset)  # never bump anyone *down* — curve is one-way
    for s in student_summaries:
        if s["percentage"] is None:
            s["curved_pct"] = None
        else:
            s["curved_pct"] = min(100, max(0, s["percentage"] + offset))
    return int(round(offset))


