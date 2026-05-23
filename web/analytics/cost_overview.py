# -*- coding: utf-8 -*-
"""Cross-cutting "AI spend this month" reader.

We do NOT duplicate AI cost data into ``analytics.db`` — it already lives in:

- xScore: ``output/xscore/grade_uploads/<job_id>/.../34_ai_costs/cost.json``
  (and similarly for non-web xScore runs under ``output/xscore/<exam>/<ts>/``).
- NL/extraction: ``<output_pdf>.parent/ai_costs/cost.json`` produced by
  ``run_extraction_jobs`` via ``eXercise.cost_report.write_cost_report``.
- eXam: ``eXam/cost_tracker.cost_breakdown(since, until)`` over the
  ``ai_calls`` SQLite table.

This module unions those three sources for the overview-card "AI spend this
month" number. It is read-time only — no writes, no caching that outlives a
single dashboard render. (Reading a handful of small JSON files + one SQL
aggregate per dashboard hit is well under 100 ms on the volumes expected.)
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# Repo root is …/eXercise/; this file is at …/eXercise/web/analytics/cost_overview.py
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_XSCORE_OUTPUT = _REPO_ROOT / "output" / "xscore"


def _iter_cost_json_files() -> list[Path]:
    """Find every ``cost.json`` under ``output/xscore/**`` (grade uploads + CLI runs).

    Globs are bounded — the directory tree is shallow (job → timestamp → step).
    Returns an empty list if the root doesn't exist yet (fresh install).
    """
    if not _XSCORE_OUTPUT.is_dir():
        return []
    return list(_XSCORE_OUTPUT.glob("**/34_ai_costs/cost.json")) + list(
        _XSCORE_OUTPUT.glob("**/ai_costs/cost.json")
    )


def _iter_nl_cost_json_files() -> list[Path]:
    """Find every ``cost.json`` under the NL output tree."""
    nl_root = _REPO_ROOT / "output"
    if not nl_root.is_dir():
        return []
    # Skip the xscore subtree (already covered) — match ai_costs/ directly under
    # any non-xscore subfolder.
    matches: list[Path] = []
    for p in nl_root.glob("**/ai_costs/cost.json"):
        try:
            p.relative_to(_XSCORE_OUTPUT)
        except ValueError:
            matches.append(p)
    return matches


def _read_cost_json(path: Path) -> tuple[float, int, int]:
    """Return ``(total_cost_rmb, mtime_epoch, age_days)`` or ``(0.0, 0, -1)`` on error.

    Age is computed from file mtime — the cost.json is written at end of run,
    so mtime ≈ run-completion time, which is what we want for "this month".
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        cost = float(data.get("total_cost_rmb") or 0.0)
        mtime = path.stat().st_mtime
        age_days = (_dt.datetime.now().timestamp() - mtime) / 86400.0
        return (cost, int(mtime), int(age_days))
    except Exception:
        _log.debug("could not read cost.json at %s", path)
        return (0.0, 0, -1)


def _file_cost_sum(*, files: list[Path], since_epoch: float) -> tuple[float, int]:
    """Sum ``total_cost_rmb`` across *files* whose mtime is ``>= since_epoch``."""
    total = 0.0
    n = 0
    for p in files:
        cost, mtime, _ = _read_cost_json(p)
        if mtime >= since_epoch and cost > 0.0:
            total += cost
            n += 1
    return (round(total, 4), n)


def total_ai_cost_rmb(*, days: int = 30) -> dict[str, Any]:
    """Union AI spend across xScore cost.json files + NL cost.json files + eXam DB.

    Returns ``{xscore: {cost, files}, nl: {cost, files}, eXam: {cost, calls},
    total_cost_rmb, since_iso, days}``. ``cost`` and ``total_cost_rmb`` are
    floats in RMB; eXam ``calls`` is the per-call count from ``ai_calls``.
    """
    cutoff = _dt.datetime.now() - _dt.timedelta(days=days)
    since_epoch = cutoff.timestamp()
    since_iso = cutoff.astimezone(_dt.UTC).isoformat()

    xscore_cost, xscore_files = _file_cost_sum(
        files=_iter_cost_json_files(), since_epoch=since_epoch
    )
    nl_cost, nl_files = _file_cost_sum(
        files=_iter_nl_cost_json_files(), since_epoch=since_epoch
    )

    exam_cost = 0.0
    exam_calls = 0
    try:
        from eXam.cost_tracker import cost_breakdown
        # cost_breakdown filters on the ai_calls.called_at ISO string; pass UTC ISO.
        bd = cost_breakdown(since=since_iso)
        exam_cost = float(bd.get("total_cost_rmb") or 0.0)
        exam_calls = int(bd.get("total_calls") or 0)
    except Exception:
        _log.debug("eXam cost_breakdown unavailable", exc_info=True)

    return {
        "xscore": {"cost_rmb": xscore_cost, "files": xscore_files},
        "nl": {"cost_rmb": nl_cost, "files": nl_files},
        "eXam": {"cost_rmb": round(exam_cost, 4), "calls": exam_calls},
        "total_cost_rmb": round(xscore_cost + nl_cost + exam_cost, 4),
        "since_iso": since_iso,
        "days": days,
    }


def _iter_cost_json_records(
    *, files: list[Path], pipeline: str, since_epoch: float
) -> Iterator[dict[str, Any]]:
    """Yield one record per (file, step, model) for cost.json files in the window.

    The shape is intentionally flat so the downstream aggregator can sum on
    any axis without re-walking the JSON. ``day`` is the UTC date of the file
    mtime (the run-completion timestamp; see :func:`_read_cost_json`).
    """
    for p in files:
        try:
            mtime = p.stat().st_mtime
            if mtime < since_epoch:
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            _log.debug("could not iterate cost.json at %s", p, exc_info=True)
            continue
        day = _dt.datetime.fromtimestamp(mtime, _dt.UTC).date().isoformat()
        for phase_key, phase in (data.get("by_step") or {}).items():
            label = phase.get("step_label") or phase_key
            for model, m in (phase.get("models") or {}).items():
                yield {
                    "pipeline": pipeline,
                    "day": day,
                    "operation": label,
                    "model": model,
                    "calls": int(m.get("calls") or 0),
                    "input_tokens": int(m.get("input_tokens") or 0),
                    "output_tokens": int(m.get("output_tokens") or 0),
                    "thinking_tokens": int(m.get("thinking_tokens") or 0),
                    "cost_rmb": float(m.get("cost_rmb") or 0.0),
                }


def _bucket(rows: Iterator[dict[str, Any]], *keys: str) -> list[dict[str, Any]]:
    """Group *rows* by the tuple of *keys*, summing numeric fields. Stable order."""
    out: dict[tuple, dict[str, Any]] = {}
    for r in rows:
        k = tuple(r.get(key) for key in keys)
        if k not in out:
            out[k] = {key: r.get(key) for key in keys} | {
                "calls": 0, "input_tokens": 0, "output_tokens": 0,
                "thinking_tokens": 0, "cost_rmb": 0.0,
            }
        agg = out[k]
        agg["calls"] += int(r.get("calls") or 0)
        agg["input_tokens"] += int(r.get("input_tokens") or 0)
        agg["output_tokens"] += int(r.get("output_tokens") or 0)
        agg["thinking_tokens"] += int(r.get("thinking_tokens") or 0)
        agg["cost_rmb"] += float(r.get("cost_rmb") or 0.0)
    return list(out.values())


def ai_spend_breakdown(*, days: int | None) -> dict[str, Any]:
    """Comprehensive cross-pipeline cost breakdown for the admin dashboard.

    ``days=None`` means "all time" — both file mtime and SQL ``called_at``
    filters fall through to a 1970 epoch. Returns:

    - ``total_*``: aggregate totals across all three pipelines
    - ``by_pipeline``: one row per pipeline (xscore / nl / eXam)
    - ``by_model``: one row per model summed across pipelines, with
      ``has_pricing`` flag (False = model missing from AI API costs.xlsx)
    - ``by_pipeline_model``: per (pipeline, model) — the headline drill-down
    - ``by_day``: per (day, pipeline) cost — feeds the stacked area chart
    - ``by_operation``: per (pipeline, operation) — feeds the collapsible drill
    - ``window``: ``{since_iso, days_effective}`` (days_effective is None for all)
    """
    if days is None or days <= 0:
        cutoff = _dt.datetime(1970, 1, 1, tzinfo=_dt.UTC)
        since_epoch = 0.0
        since_iso = cutoff.isoformat()
    else:
        cutoff = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)
        since_epoch = cutoff.timestamp()
        since_iso = cutoff.isoformat()

    records: list[dict[str, Any]] = []

    # xScore + NL: walk cost.json files.
    records.extend(_iter_cost_json_records(
        files=_iter_cost_json_files(), pipeline="xscore", since_epoch=since_epoch,
    ))
    records.extend(_iter_cost_json_records(
        files=_iter_nl_cost_json_files(), pipeline="nl", since_epoch=since_epoch,
    ))

    # eXam: per-(day, model) from the ai_calls table; we attribute every eXam
    # record to operation="eXam (all ops)" in the by_operation bucket and rely
    # on /eXam/teacher/costs for the per-operation drill since the admin view
    # doesn't need per-operation eXam (it's already in the teacher view).
    # But we DO want per-operation eXam in the admin's by_operation table for
    # parity with xScore — so we also fetch cost_breakdown's by_operation.
    try:
        from eXam.cost_tracker import cost_breakdown, cost_by_day_model

        exam_by_day = cost_by_day_model(since=since_iso)
        for r in exam_by_day:
            records.append({
                "pipeline": "eXam",
                "day": r["day"],
                "operation": None,  # filled below from cost_breakdown
                "model": r["model"],
                "calls": int(r["calls"] or 0),
                "input_tokens": int(r["input_tokens"] or 0),
                "output_tokens": int(r["output_tokens"] or 0),
                "thinking_tokens": int(r["thinking_tokens"] or 0),
                "cost_rmb": float(r["cost_rmb"] or 0.0),
            })
        exam_ops = cost_breakdown(since=since_iso).get("by_operation") or []
    except Exception:
        _log.debug("eXam cost queries unavailable", exc_info=True)
        exam_ops = []

    # Pricing-coverage check: any model that appears with non-zero token usage
    # but is missing from pricing is flagged so the owner can update the xlsx.
    try:
        from eXercise.cost_report import _load_pricing
        pricing = _load_pricing()
    except Exception:
        pricing = {}

    by_pipeline = _bucket(iter(records), "pipeline")
    by_model = _bucket(iter(records), "model")
    by_pipeline_model = _bucket(iter(records), "pipeline", "model")
    by_day_pipeline = _bucket(iter(records), "day", "pipeline")

    # Operations: pull per-step from xScore/NL records (they carry operation),
    # and per-operation from eXam's cost_breakdown (separate query result).
    op_rows = [r for r in records if r.get("operation")]
    by_operation = _bucket(iter(op_rows), "pipeline", "operation")
    for r in exam_ops:
        by_operation.append({
            "pipeline": "eXam",
            "operation": r["operation"],
            "calls": int(r["calls"] or 0),
            "input_tokens": int(r["input_tokens"] or 0),
            "output_tokens": int(r["output_tokens"] or 0),
            "thinking_tokens": int(r["thinking_tokens"] or 0),
            "cost_rmb": float(r["total_cost_rmb"] or 0.0),
        })

    # Annotate by_model with pricing coverage.
    for m in by_model:
        m["has_pricing"] = m["model"] in pricing if m["model"] else True

    # Round + sort for display.
    def _finalize(rows: list[dict[str, Any]], sort_by: str = "cost_rmb") -> list[dict[str, Any]]:
        for r in rows:
            r["cost_rmb"] = round(float(r.get("cost_rmb") or 0.0), 4)
        return sorted(rows, key=lambda r: r.get(sort_by, 0), reverse=True)

    by_day = sorted(by_day_pipeline, key=lambda r: (r["day"] or "", r["pipeline"] or ""))
    for r in by_day:
        r["cost_rmb"] = round(float(r.get("cost_rmb") or 0.0), 4)

    totals = {
        "cost_rmb": round(sum(r["cost_rmb"] for r in by_pipeline), 4),
        "calls": sum(r["calls"] for r in by_pipeline),
        "input_tokens": sum(r["input_tokens"] for r in by_pipeline),
        "output_tokens": sum(r["output_tokens"] for r in by_pipeline),
        "thinking_tokens": sum(r["thinking_tokens"] for r in by_pipeline),
    }

    return {
        "total_cost_rmb": totals["cost_rmb"],
        "total_calls": totals["calls"],
        "total_input_tokens": totals["input_tokens"],
        "total_output_tokens": totals["output_tokens"],
        "total_thinking_tokens": totals["thinking_tokens"],
        "by_pipeline": _finalize(by_pipeline),
        "by_model": _finalize(by_model),
        "by_pipeline_model": _finalize(by_pipeline_model),
        "by_operation": _finalize(by_operation),
        "by_day": by_day,
        "window": {"since_iso": since_iso, "days_effective": days},
    }


def rollup_from_cost_json(path: Path) -> dict[str, Any]:
    """Parse a cost.json into a compact rollup for event properties.

    Returns ``{ai_cost_rmb, ai_calls, ai_models}``. Missing file, malformed
    JSON, or unexpected shape → zeroed defaults; never raises.

    Calls are summed across ``by_step[*].models[*].calls`` because the
    top-level ``token_usage`` block doesn't carry per-call counts (only token
    totals + cost).
    """
    out: dict[str, Any] = {"ai_cost_rmb": 0.0, "ai_calls": 0, "ai_models": []}
    try:
        p = Path(path)
        if not p.is_file():
            return out
        data = json.loads(p.read_text(encoding="utf-8"))
        out["ai_cost_rmb"] = round(float(data.get("total_cost_rmb") or 0.0), 4)
        token_usage = data.get("token_usage") or {}
        out["ai_models"] = sorted(token_usage.keys())
        calls = 0
        for phase in (data.get("by_step") or {}).values():
            for model_data in (phase.get("models") or {}).values():
                calls += int(model_data.get("calls") or 0)
        out["ai_calls"] = calls
    except Exception:
        _log.debug("rollup_from_cost_json failed for %s", path, exc_info=True)
    return out
