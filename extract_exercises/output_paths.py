# -*- coding: utf-8 -*-
"""Run output directory and bare-filename resolution."""

import datetime
from pathlib import Path

from .config import OUTPUT_DIR

_CURRENT_RUN_DIR: Path | None = None


def _create_run_dir(label: str | None = None) -> Path:
    """Create a new run directory under output/.

    Folder name: ``<label>_<YYYY-MM-DD>_<HH-MM-SS>`` when *label* is given
    (e.g. ``physics_s25_q38-40_2026-03-26_14-30-22``), otherwise the plain
    date-time string ``<YYYY-MM-DD>_<HH-MM-SS>``.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H-%M-%S")
    folder_name = f"{label}_{stamp}" if label else stamp
    run_dir = OUTPUT_DIR / folder_name
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {run_dir}")
    return run_dir


def ensure_run_output_dir(label: str | None = None) -> Path:
    """Return the current run directory, creating it on first call.

    *label* is only used when the directory has not yet been created; subsequent
    calls return the already-created directory regardless of *label*.
    """
    global _CURRENT_RUN_DIR
    if _CURRENT_RUN_DIR is None:
        _CURRENT_RUN_DIR = _create_run_dir(label)
    return _CURRENT_RUN_DIR


def fresh_run_output_dir(label: str | None = None) -> Path:
    """Create a new run directory for each call (web requests where each job needs its own)."""
    return _create_run_dir(label)


def resolve_output_path(output_pdf: str) -> Path:
    """Bare filenames → ``output/<stem>_<timestamp>/``; absolute or nested relative paths unchanged."""
    p = Path(output_pdf)
    if p.is_absolute() or p.parent != Path("."):
        return p
    return ensure_run_output_dir(label=p.stem) / p.name


def resolve_output_path_fresh(output_pdf: str) -> Path:
    """Bare filenames → new ``output/<stem>_<timestamp>/`` for each call (web UI); absolute paths unchanged."""
    p = Path(output_pdf)
    if p.is_absolute() or p.parent != Path("."):
        return p
    return fresh_run_output_dir(label=p.stem) / p.name
