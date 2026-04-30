"""Structured per-step JSONL log + run manifest.

Two artifacts, both rooted at ``ctx.artifact_dir``:

* ``run.log.jsonl`` — one JSON object per line, appended as steps complete.
  Schema (all keys optional unless noted):
      ts            ISO-8601 UTC timestamp                     (always)
      step_number   1-based ordinal (1–25)                     (always)
      step_name     snake_case identifier                      (always)
      status        "ok" | "error" | "cache_hit" | …           (always)
      duration_s    wall-clock duration in seconds             (when measured)
      model         model identifier ("qwen3.6-plus", …)       (AI calls)
      tokens_in     prompt tokens                              (AI calls)
      tokens_out    completion tokens                          (AI calls)
      student       student name                               (per-student events)
      page          1-based page within student booklet        (per-page events)
      error         "<ExceptionType>: <message>"               (on error)
      extra         free-form dict for sub-step metadata       (anything else)

* ``run.json`` — single JSON object, written once at run end.
  Schema:
      git_sha            "<40-char>" or "" if unavailable
      timestamp_started  ISO-8601 UTC at pipeline start
      timestamp_finished ISO-8601 UTC at pipeline end
      duration_s         total wall-clock duration
      status             "ok" | "early_exit" | "error"
      step_count         number of steps in the registry
      failed_steps       list of {step_number, step_name, error} (subset of step_failures)
      prompt_versions    {prompt_name: version}
      config             selected env-var snapshot (no secrets)
      total_cost_rmb     None until the cost report has run
      ai_temperature     float | None
      ai_seed            int | None

Both files are best-effort: write failures are swallowed (logged once via
``warn_line``) so that a flaky filesystem doesn't crash the pipeline.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xscore.shared.pipeline_ctx import _Ctx


_LOG_FILE_NAME = "run.log.jsonl"
_MANIFEST_FILE_NAME = "run.json"
_CONFIG_KEYS_TO_SNAPSHOT = (
    "AI_DEFAULT_MODEL",
    "ALL_AI_TEMPERATURE",
    "ALL_AI_SEED",
    "ALL_AI_OUTPUT_FORMAT",
    "INTERPRET_PROMPT_MODEL",
    "READ_STUDENT_LIST_MODEL",
    "EMPTY_EXAM_COVER_MODEL",
    "COVER_PAGE_DETECTION_MODEL",
    "NAME_DETECTION_MODEL",
    "PAGE_ORDER_CHECK_MODEL",
    "EXAM_BLANK_DETECTION_MODEL",
    "HANDWRITING_CHECK_MODEL",
    "DETECT_LAYOUT_MODEL",
    "READ_EXAM_PDF_MODEL",
    "DETECT_SCHEME_GRAPHICS_MODEL",
    "READ_MARK_SCHEME_MODEL",
    "MARKING_MODEL",
    "MARKING_DPI",
    "MARKING_WORKERS",
    "PIPELINE_DEFAULT_DPI",
    "EXAM_PROFILE",
)

_log_lock = threading.Lock()
_warned_about_log_failure = False


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds")


def _safe_warn_once(msg: str) -> None:
    global _warned_about_log_failure
    if _warned_about_log_failure:
        return
    _warned_about_log_failure = True
    try:
        from xscore.shared.terminal_ui import warn_line
        warn_line(msg)
    except Exception:
        pass


def _log_path(ctx: "_Ctx") -> Path | None:
    if ctx.artifact_dir is None:
        return None
    return ctx.artifact_dir / _LOG_FILE_NAME


def log_step_event(
    ctx: "_Ctx",
    *,
    step_number: int,
    step_name: str,
    status: str,
    duration_s: float | None = None,
    model: str | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    student: str | None = None,
    page: int | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one structured event line to ``run.log.jsonl``.

    Thread-safe (used by parallel marking workers). Best-effort: write errors
    are warned once and swallowed.
    """
    path = _log_path(ctx)
    if path is None:
        return
    record: dict[str, Any] = {
        "ts":          _now_iso(),
        "step_number": step_number,
        "step_name":   step_name,
        "status":      status,
    }
    if duration_s is not None:
        record["duration_s"] = round(duration_s, 4)
    if model is not None:
        record["model"] = model
    if tokens_in is not None:
        record["tokens_in"] = tokens_in
    if tokens_out is not None:
        record["tokens_out"] = tokens_out
    if student is not None:
        record["student"] = student
    if page is not None:
        record["page"] = page
    if error is not None:
        record["error"] = error
    if extra:
        record["extra"] = extra

    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with _log_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line)
    except OSError as exc:
        _safe_warn_once(f"run_log: failed to append event ({exc}); subsequent events suppressed")


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=False, capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return ""


def _config_snapshot() -> dict[str, str]:
    """Return env-var values for inspection-grade reproducibility (no secrets)."""
    return {k: os.environ.get(k, "") for k in _CONFIG_KEYS_TO_SNAPSHOT}


def _coerce_temperature() -> float | None:
    raw = os.environ.get("ALL_AI_TEMPERATURE", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _coerce_seed() -> int | None:
    raw = os.environ.get("ALL_AI_SEED", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def write_run_manifest(
    ctx: "_Ctx",
    *,
    status: str,
    timestamp_started: str,
    duration_s: float | None = None,
) -> None:
    """Write ``run.json`` at the artifact root. Best-effort; swallows IO errors."""
    if ctx.artifact_dir is None:
        return
    try:
        from xscore.prompts.loader import all_prompt_versions
    except Exception:
        prompt_versions: dict[str, str] = {}
    else:
        try:
            prompt_versions = all_prompt_versions()
        except Exception:
            prompt_versions = {}

    failed_steps = [
        {k: v for k, v in f.items() if k in ("step_number", "step_name", "error")}
        for f in (ctx.step_failures or [])
    ]

    # Total cost is written by ai_costs to ai_costs/cost.json. Surface it in
    # the manifest if available; fall back to the legacy timing-summary location
    # for runs done before the cost-report split.
    total_cost_rmb: float | None = None
    try:
        from xscore.shared.exam_paths import (
            artifact_cost_json_path,
            artifact_timing_json_path,
        )
        for _p in (artifact_cost_json_path(ctx.artifact_dir),
                   artifact_timing_json_path(ctx.artifact_dir)):
            if _p.is_file():
                data = json.loads(_p.read_text(encoding="utf-8"))
                total_cost_rmb = data.get("total_cost_rmb")
                if total_cost_rmb is not None:
                    break
    except Exception:
        pass

    try:
        from xscore.shared.pipeline_steps import STEPS
        step_count = len(STEPS)
    except Exception:
        step_count = None

    manifest: dict[str, Any] = {
        "git_sha":            _git_sha(),
        "timestamp_started":  timestamp_started,
        "timestamp_finished": _now_iso(),
        "duration_s":         round(duration_s, 4) if duration_s is not None else None,
        "status":             status,
        "step_count":         step_count,
        "failed_steps":       failed_steps,
        "prompt_versions":    prompt_versions,
        "config":             _config_snapshot(),
        "total_cost_rmb":     total_cost_rmb,
        "ai_temperature":     _coerce_temperature(),
        "ai_seed":            _coerce_seed(),
    }

    try:
        path = ctx.artifact_dir / _MANIFEST_FILE_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        _safe_warn_once(f"run_log: failed to write run.json ({exc})")
