# -*- coding: utf-8 -*-
"""Run output directory and bare-filename resolution."""

from pathlib import Path

from .config import OUTPUT_DIR

_CURRENT_RUN_DIR: Path | None = None
_RUN_COMMAND: str | None = None


def set_run_command(command: str) -> None:
    """Store the command/prompt that initiated this run (written into the output dir)."""
    global _RUN_COMMAND
    _RUN_COMMAND = command


def _write_command_txt(run_dir: Path) -> None:
    """Write command.txt into *run_dir* if a command has been stored."""
    if _RUN_COMMAND:
        (run_dir / "command.txt").write_text(_RUN_COMMAND, encoding="utf-8")


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
    """
    global _CURRENT_RUN_DIR
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
