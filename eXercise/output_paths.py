# -*- coding: utf-8 -*-
"""Run output directory and bare-filename resolution."""

import contextlib
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path

from .config import OUTPUT_DIR


@contextlib.contextmanager
def temp_pdf_path() -> Iterator[Path]:
    """Yield a unique temporary ``.pdf`` path; unlink on exit (best-effort).

    Used by callers that need to round-trip PDF bytes through a file path
    (e.g. for an SDK that only accepts paths, or to read back a PDF the
    layout engine just wrote).  Suppresses ``OSError`` on cleanup so an
    already-deleted tmp file or AV-locked path does not leak into the
    pipeline as a fatal error.
    """
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        path = Path(f.name)
    try:
        yield path
    finally:
        try:
            path.unlink()
        except OSError:
            pass

_CURRENT_RUN_DIR: Path | None = None
_CURRENT_RUN_DIR_LOCK = threading.Lock()
_tls = threading.local()


def set_run_command(command: str) -> None:
    """Store the command/prompt that initiated this run (written into the output dir)."""
    _tls.run_command = command


def _write_command_txt(run_dir: Path) -> None:
    """Write command.txt into *run_dir* if a command has been stored."""
    cmd = getattr(_tls, "run_command", None)
    if cmd:
        (run_dir / "command.txt").write_text(cmd, encoding="utf-8")


def _create_run_dir(label: str | None = None) -> Path:
    """Create a new run directory under output/.

    Folder name: *label* when given, otherwise ``run``.
    If the folder already exists, append `` 2``, `` 3``, etc.
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    base = label if label else "run"
    # Try base name first, then suffixed candidates — use mkdir(exist_ok=False)
    # so concurrent processes can't both succeed on the same name.
    for candidate in [OUTPUT_DIR / base] + [OUTPUT_DIR / f"{base} {n}" for n in range(2, 10000)]:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            print(f"Output directory: {candidate}")
            _write_command_txt(candidate)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError(f"Could not create a unique output directory for label={base!r}")


def ensure_run_output_dir(label: str | None = None) -> Path:
    """Return the current run directory, creating it on first call.

    *label* is only used when the directory has not yet been created; subsequent
    calls return the already-created directory regardless of *label*.

    Synchronised so concurrent first-callers don't each create separate
    ``run/`` and ``run 2/`` directories.
    """
    global _CURRENT_RUN_DIR
    if _CURRENT_RUN_DIR is not None:
        return _CURRENT_RUN_DIR
    with _CURRENT_RUN_DIR_LOCK:
        if _CURRENT_RUN_DIR is None:
            _CURRENT_RUN_DIR = _create_run_dir(label)
        return _CURRENT_RUN_DIR


def fresh_run_output_dir(label: str | None = None) -> Path:
    """Create a new run directory for each call (web requests where each job needs its own)."""
    return _create_run_dir(label)


def resolve_output_path(output_pdf: str) -> Path:
    """Bare filenames → ``output/<stem>/``; absolute or nested relative paths unchanged."""
    p = Path(output_pdf)
    if p.is_absolute() or p.parent != Path("."):
        return p
    return ensure_run_output_dir(label=p.stem) / p.name


def resolve_output_path_fresh(output_pdf: str) -> Path:
    """Bare filenames → new ``output/<stem>/`` for each call (web UI); absolute paths unchanged."""
    p = Path(output_pdf)
    if p.is_absolute() or p.parent != Path("."):
        return p
    return fresh_run_output_dir(label=p.stem) / p.name
