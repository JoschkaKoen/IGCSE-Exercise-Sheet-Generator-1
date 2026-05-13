"""Path builders for end-of-run summary artifacts (timing + AI cost)."""

from __future__ import annotations

from pathlib import Path

from xscore.shared.step_folders import AI_COSTS_DIR, TIMING_DIR


# ---------------------------------------------------------------------------
# Timing summary (timing only, no accuracy/cost)
# ---------------------------------------------------------------------------

def artifact_timing_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / TIMING_DIR / "timing.json"


def artifact_timing_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / TIMING_DIR / "timing.md"


# ---------------------------------------------------------------------------
# AI costs
# ---------------------------------------------------------------------------

def artifact_cost_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / AI_COSTS_DIR / "cost.json"


def artifact_cost_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / AI_COSTS_DIR / "cost.md"
