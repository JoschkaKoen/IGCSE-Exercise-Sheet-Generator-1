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
