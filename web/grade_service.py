# -*- coding: utf-8 -*-
"""Run the xScore scan pipeline (steps 1, 3–8) for the web grade worker thread.

Steps 1 (parse prompt) and 3 (load roster) are quick; steps 4–5 (scaffold) are
optional and run if an ``empty_exam.pdf`` was uploaded; steps 6–8 (blank
detection, autorotate, deskew) do the heavy scan processing.

Step 2 (find folder) is bypassed because the folder is already known from the
uploaded files.
"""

from __future__ import annotations

import datetime
import time
from collections.abc import Callable
from pathlib import Path


def run_scan_pipeline(
    folder: Path,
    prompt: str | None,
    on_line: Callable[[str], None],
    *,
    dpi: int | None = None,
    force_clean_scan: bool = False,
) -> Path:
    """Run steps 1, 3, 5–7 against *folder* (uploaded exam files).

    Args:
        folder: Directory that contains ``scan.pdf`` and ``StudentList.*``.
        prompt: Optional NL instruction (used to extract DPI / task options).
        on_line: Callback invoked with each progress line (for web job streaming).
        dpi: Override DPI (takes precedence over prompt-parsed value).
        force_clean_scan: Rebuild cleaned scan even if cached.

    Returns:
        Path to ``3_cleaned_scan.pdf`` inside the run's artifact directory.
    """
    from .process_log import run_with_last_log_line

    from xscore.marking.parse_instruction import parse_prompt
    from xscore.preprocessing.start_scan import (
        CLEANED_SCAN_PDF,
        autorotate_phase,
        deskew_phase,
        detect_blank_pages_phase,
        find_source_scan_match,
    )
    from xscore.shared.load_student_list import read_student_list
    from xscore.shared.terminal_ui import pipeline_step

    def emit(msg: str) -> None:
        on_line(msg)

    # ------------------------------------------------------------------ step 1
    effective_dpi = dpi or 400
    if prompt and prompt.strip():
        emit("Step 1 — AI API call: Parse grading instructions…")
        try:
            instruction = run_with_last_log_line(
                lambda: parse_prompt(prompt, dpi_override=dpi),
                on_line,
            )
            if dpi is None:
                effective_dpi = instruction.dpi
        except Exception as exc:  # noqa: BLE001
            emit(f"Step 1 — Prompt parse failed ({exc}); using defaults.")
    else:
        emit("Step 1 — No prompt provided; using default settings.")

    # step 2 skipped on web route — folder is already known from the upload

    # ------------------------------------------------------------------ step 3
    emit("Step 3 — Loading student roster…")
    try:
        students = read_student_list(folder)
        emit(f"Step 3 — {len(students)} students on the roster.")
    except FileNotFoundError:
        emit("Step 3 — No student list found (continuing without roster).")

    # ------------------------------------------------------------------ artifact dir
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    artifact_dir = folder / timestamp
    suffix = 1
    while artifact_dir.exists():
        suffix += 1
        artifact_dir = folder / f"{timestamp}_{suffix}"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ steps 4–5
    empty_exam_path = folder / "empty_exam.pdf"
    if empty_exam_path.is_file():
        emit("Step 4 — AI API call: Parse exam PDF…")
        try:
            from xscore.scaffold.generate_scaffold import build_scaffold

            def _on_exam_done(raw_questions: list) -> None:
                emit(f"Step 4 — {len(raw_questions)} top-level questions extracted.")
                emit("Step 5 — AI API call: Parse mark scheme…")

            scaffold = build_scaffold(
                folder,
                artifact_dir=artifact_dir,
                exam_pdf_override=empty_exam_path,
                on_exam_complete=_on_exam_done,
            )
            emit(
                f"Step 5 — {len(scaffold.gradable_questions)} gradable parts"
                f"  ·  {scaffold.total_marks} marks."
            )
        except Exception as exc:  # noqa: BLE001
            emit(f"Step 4 — Scaffold skipped: {exc}")
    else:
        emit("Step 4 — No empty exam uploaded; scaffold skipped.")

    # ------------------------------------------------------------------ steps 6–8
    cleaned_path = artifact_dir / CLEANED_SCAN_PDF

    emit("Step 6 — Locating scan PDF…")
    source_scan = find_source_scan_match(folder, artifact_dir, effective_dpi)

    emit("Step 6 — Detecting blank pages…")
    run_with_last_log_line(
        lambda: detect_blank_pages_phase(
            source_scan,
            artifact_dir,
            analysis_dpi=effective_dpi,
            force_clean_scan=force_clean_scan,
        ),
        on_line,
    )

    emit("Step 7 — Autorotating…")
    run_with_last_log_line(lambda: autorotate_phase(artifact_dir), on_line)

    emit("Step 8 — Deskewing…")
    run_with_last_log_line(
        lambda: deskew_phase(folder, artifact_dir, effective_dpi),
        on_line,
    )

    if not cleaned_path.is_file():
        raise RuntimeError(f"Deskew completed but output not found: {cleaned_path}")

    emit(f"Done — cleaned scan saved.")
    return cleaned_path


def run_scan_pipeline_logged(
    folder: Path,
    prompt: str | None,
    on_line: Callable[[str], None],
    **kwargs,
) -> Path:
    """Wrapper that times the full pipeline and logs elapsed time."""
    t0 = time.perf_counter()
    try:
        result = run_scan_pipeline(folder, prompt, on_line, **kwargs)
        elapsed = time.perf_counter() - t0
        on_line(f"Pipeline finished in {elapsed:.1f}s.")
        return result
    except Exception:
        elapsed = time.perf_counter() - t0
        on_line(f"Pipeline failed after {elapsed:.1f}s.")
        raise
