# -*- coding: utf-8 -*-
"""Run output directory and bare-filename resolution."""

import datetime
from pathlib import Path

from .config import OUTPUT_DIR

_CURRENT_RUN_DIR: Path | None = None


def _create_run_dir() -> Path:
    """Create a new timestamped run directory under output/."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = OUTPUT_DIR / f"run_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {run_dir}")
    return run_dir


def ensure_run_output_dir() -> Path:
    """Create ``output/run_<timestamp>/`` once per script run; return that directory."""
    global _CURRENT_RUN_DIR
    if _CURRENT_RUN_DIR is None:
        _CURRENT_RUN_DIR = _create_run_dir()
    return _CURRENT_RUN_DIR


def fresh_run_output_dir() -> Path:
    """Create a new ``output/run_<timestamp>/`` directory (for web requests where each job needs its own)."""
    return _create_run_dir()


def resolve_output_path(output_pdf: str) -> Path:
    """Bare filenames → ``output/run_<timestamp>/``; absolute or nested relative paths unchanged."""
    p = Path(output_pdf)
    if p.is_absolute() or p.parent != Path("."):
        return p
    return ensure_run_output_dir() / p.name


def resolve_output_path_fresh(output_pdf: str) -> Path:
    """Bare filenames → new ``output/run_<timestamp>/`` for each call (web UI); absolute paths unchanged."""
    p = Path(output_pdf)
    if p.is_absolute() or p.parent != Path("."):
        return p
    return fresh_run_output_dir() / p.name
