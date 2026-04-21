# -*- coding: utf-8 -*-
"""Run the full xScore pipeline (steps 1–14) for the web grade worker thread.

Step 2 (find folder) is bypassed — the folder is already known from the uploaded files.
Order matches ``xScore.py``: steps 4–6 scan cleaning, step 7 exam geometry + name assignment,
steps 8–10 scaffold (requires ``empty_exam.pdf``), steps 11–14 marking and reports.
"""

from __future__ import annotations

import datetime
import logging
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace


def run_full_pipeline(
    folder: Path,
    prompt: str | None,
    on_line: Callable[[str], None],
    on_step: Callable[[int, str, float | None], None],
    *,
    dpi: int | None = None,
) -> tuple[Path, Path]:
    """Run all 14 xScore pipeline steps against *folder* (uploaded exam files).

    on_step(num, event, elapsed_s) is called for each step state change.
    event is one of: "running", "done", "failed".

    Returns (cleaned_pdf, artifact_dir).
    """
    from xscore.marking.ai_mark import run_ai_marking
    from xscore.marking.assign_pages_to_students import (
        assign_pages,
        page_assignments_to_json,
        page_assignments_to_md,
    )
    from xscore.marking.blueprints import build_blueprints
    from xscore.marking.geometry import compute_geometry, write_geometry_artifacts
    from xscore.marking.merge_reports import compile_reports
    from xscore.marking.parse_instruction import _heuristic_fallback, parse_prompt
    from xscore.marking.timing_report import write_timing_report
    from xscore.config import ROTATION_ANALYSIS_DPI
    from xscore.preprocessing.start_scan import (
        autorotate_phase,
        deskew_phase,
        detect_blank_pages_phase,
        find_source_scan_match,
    )
    from xscore.scaffold.generate_scaffold import build_scaffold
    from xscore.shared.exam_paths import (
        artifact_exam_student_list_json_path,
        artifact_exam_student_list_md_path,
        validate_input_files,
    )
    from xscore.shared.load_student_list import read_student_list

    step_timings: dict[str, float] = {}

    def _step(num: int, fn: Callable) -> any:
        """Call fn() wrapped with on_step events; record duration in *step_timings*."""
        t0 = time.perf_counter()
        on_step(num, "running", None)
        try:
            result = fn()
            elapsed = round(time.perf_counter() - t0, 2)
            step_timings[f"step_{num}_s"] = elapsed
            on_step(num, "done", elapsed)
            return result
        except Exception:
            on_step(num, "failed", round(time.perf_counter() - t0, 2))
            raise

    # ---------------------------------------------------------------------- step 1
    effective_dpi = dpi or 400
    instruction = _heuristic_fallback(prompt or "", dpi_override=dpi)
    if prompt and prompt.strip():
        on_line("Step 1 — Parsing grading instructions…")
        t0_1 = time.perf_counter()
        on_step(1, "running", None)
        try:
            instruction = parse_prompt(prompt, dpi_override=dpi)
            if dpi is None:
                effective_dpi = instruction.dpi
            on_step(1, "done", round(time.perf_counter() - t0_1, 2))
        except Exception as exc:  # noqa: BLE001
            logging.exception("Step 1 prompt parse failed")
            on_line(f"Step 1 — Prompt parse error ({exc}); using heuristic defaults.")
            on_step(1, "done", round(time.perf_counter() - t0_1, 2))
    else:
        t0_1 = time.perf_counter()
        on_step(1, "running", None)
        on_line("Step 1 — No prompt; using defaults.")
        on_step(1, "done", round(time.perf_counter() - t0_1, 2))

    # ---------------------------------------------------------------------- step 2
    # Folder is already known from upload; validate required input files.
    on_step(2, "running", None)
    try:
        validate_input_files(folder)
        on_step(2, "done", 0.0)
    except FileNotFoundError as exc:
        on_line(f"Step 2 — {exc}")
        on_step(2, "failed", 0.0)
        raise

    # ---------------------------------------------------------------------- step 3
    on_line("Step 3 — Loading student roster…")
    students: list[str] = []

    def _load_roster() -> list[str]:
        result = read_student_list(folder)
        on_line(f"Step 3 — {len(result)} students on the roster.")
        return result

    students = _step(3, _load_roster)

    # ---------------------------------------------------------------------- artifact dir
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    artifact_dir = folder / f"{timestamp}_{uuid.uuid4().hex[:8]}"
    artifact_dir.mkdir(parents=True, exist_ok=False)
    _cmd_parts = ["web"]
    if prompt and prompt.strip():
        _cmd_parts.append(prompt.strip())
    if dpi is not None:
        _cmd_parts.append(f"--dpi {dpi}")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "command.txt").write_text(" ".join(_cmd_parts), encoding="utf-8")

    # ---------------------------------------------------------------------- steps 4–6 (scan cleaning — same order as xScore.py)
    empty_exam_path = folder / "empty_exam.pdf"
    scaffold = None

    on_line("Step 4 — Detecting blank pages…")
    source_scan = find_source_scan_match(folder, artifact_dir, effective_dpi)

    def _blank() -> None:
        detect_blank_pages_phase(
            source_scan,
            artifact_dir,
            analysis_dpi=ROTATION_ANALYSIS_DPI,
            force_clean_scan=True,
        )

    _step(4, _blank)
    on_line("Step 4 — Blank pages detected.")

    def _rotate() -> None:
        autorotate_phase(artifact_dir)

    on_line("Step 5 — Autorotating…")
    _step(5, _rotate)

    on_line("Step 6 — Deskewing…")

    def _deskew() -> Path:
        return deskew_phase(folder, artifact_dir, effective_dpi)

    cleaned_pdf: Path = _step(6, _deskew)
    on_line("Step 6 — Cleaned scan ready.")

    # ---------------------------------------------------------------------- step 7 (geometry + name assignment — before scaffold)
    on_line("Step 7 — Computing geometry and assigning pages to students (AI)…")

    def _empty_exam_page_count() -> int:
        import fitz

        if not empty_exam_path.is_file():
            raise FileNotFoundError(
                "empty_exam.pdf is required but was not uploaded. "
                "Please re-submit with the blank exam PDF attached."
            )
        with fitz.open(str(empty_exam_path)) as doc:
            return doc.page_count

    exam_pages = _empty_exam_page_count()

    def _geometry() -> dict:
        geo = compute_geometry(cleaned_pdf, exam_pages, students)
        write_geometry_artifacts(artifact_dir, geo)
        return geo

    t7 = time.perf_counter()
    on_step(7, "running", None)
    try:
        geo = _geometry()
        pages_per_student: int = geo["pages_per_student"]
        num_students: int = geo["num_students"]

        page_assignments = assign_pages(
            cleaned_pdf,
            students,
            pages_per_student=pages_per_student,
            artifact_dir=artifact_dir,
        )
        artifact_exam_student_list_json_path(artifact_dir).write_text(
            page_assignments_to_json(page_assignments), encoding="utf-8"
        )
        artifact_exam_student_list_md_path(artifact_dir).write_text(
            page_assignments_to_md(page_assignments), encoding="utf-8"
        )
        elapsed_7 = round(time.perf_counter() - t7, 2)
        step_timings["step_7_s"] = elapsed_7
        on_step(7, "done", elapsed_7)
        on_line(f"Step 7 — {len(page_assignments)} students detected from scan.")
    except Exception:
        on_step(7, "failed", round(time.perf_counter() - t7, 2))
        raise

    # ---------------------------------------------------------------------- steps 8–10 (scaffold)
    t8_start: list[float] = []
    t9_start: list[float] = []

    def _on_exam_done(raw_questions: list) -> None:
        elapsed = round(time.perf_counter() - t8_start[0], 2) if t8_start else 0.0
        step_timings["step_8_s"] = elapsed
        on_step(8, "done", elapsed)
        on_line(f"Step 8 — {len(raw_questions)} top-level questions extracted.")
        on_step(9, "running", None)
        t9_start.append(time.perf_counter())

    def _on_scheme_done(scheme_questions: list) -> None:
        elapsed = round(time.perf_counter() - t9_start[0], 2) if t9_start else 0.0
        step_timings["step_9_s"] = elapsed
        on_step(9, "done", elapsed)
        on_line(f"Step 9 — {len(scheme_questions)} answers in mark scheme.")
        on_step(10, "running", None)

    def _build() -> any:
        t8_start.append(time.perf_counter())
        on_step(8, "running", None)
        on_line("Step 8 — Parsing exam PDF (AI)…")
        result = build_scaffold(
            folder,
            artifact_dir=artifact_dir,
            force_rebuild=True,
            exam_pdf_override=empty_exam_path,
            on_exam_complete=_on_exam_done,
            on_scheme_complete=_on_scheme_done,
            students=students,
        )
        return result

    t10_0 = time.perf_counter()
    try:
        scaffold = _build()
        elapsed_10 = round(time.perf_counter() - t10_0, 2)
        on_step(10, "done", elapsed_10)
        step_timings["step_10_s"] = elapsed_10
        on_line(
            f"Step 10 — {len(scaffold.gradable_questions)} gradable parts  ·  "
            f"{scaffold.total_marks} marks."
        )
    except Exception:
        if "step_8_s" not in step_timings:
            on_step(8, "failed", 0.0)
        if "step_9_s" not in step_timings:
            on_step(9, "failed", 0.0)
        if "step_10_s" not in step_timings:
            on_step(10, "failed", 0.0)
        raise

    # ---------------------------------------------------------------------- step 11
    on_line("Step 11 — Building marking blueprints…")

    def _blueprints() -> list:
        bps = build_blueprints(scaffold, artifact_dir)
        on_line(f"Step 11 — {len(bps)} page blueprint(s) written.")
        return bps

    _step(11, _blueprints)

    # ---------------------------------------------------------------------- step 12
    on_line("Step 12 — AI marking…")
    ctx = SimpleNamespace(
        cleaned_pdf=cleaned_pdf,
        artifact_dir=artifact_dir,
        scaffold=scaffold,
        pages_per_student=pages_per_student,
        num_students=num_students,
        instruction=instruction,
        marking_failures=[],
    )

    def _mark() -> list:
        return run_ai_marking(ctx, dpi=effective_dpi)

    api_calls: list[dict] = _step(12, _mark)
    on_line(f"Step 12 — {len(api_calls)} API calls completed.")

    # ---------------------------------------------------------------------- step 13
    if instruction.no_report:
        on_line("Step 13 — Skipped (no_report requested).")
        on_step(13, "running", None)
        on_step(13, "done", 0.0)
    else:
        on_line("Step 13 — Compiling reports…")

        def _reports() -> list:
            summaries = compile_reports(ctx)
            on_line(f"Step 13 — {len(summaries)} student report(s) written.")
            return summaries

        _step(13, _reports)

    # ---------------------------------------------------------------------- step 14
    on_line("Step 14 — Writing timing summary…")

    def _timing() -> None:
        write_timing_report(artifact_dir, step_timings, api_calls)

    _step(14, _timing)

    on_line("Done — all 14 steps complete.")
    return cleaned_pdf, artifact_dir


def run_full_pipeline_logged(
    folder: Path,
    prompt: str | None,
    on_line: Callable[[str], None],
    on_step: Callable[[int, str, float | None], None],
    **kwargs,
) -> tuple[Path, Path]:
    """Wrapper that times the full pipeline and logs elapsed time."""
    t0 = time.perf_counter()
    try:
        result = run_full_pipeline(folder, prompt, on_line, on_step, **kwargs)
        elapsed = time.perf_counter() - t0
        on_line(f"Pipeline finished in {elapsed:.1f}s.")
        return result
    except Exception:
        elapsed = time.perf_counter() - t0
        on_line(f"Pipeline failed after {elapsed:.1f}s.")
        raise
