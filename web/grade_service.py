# -*- coding: utf-8 -*-
"""Run the full xScore pipeline (steps 1–14) for the web grade worker thread.

Step 2 (find folder) is bypassed — the folder is already known from the uploaded files.
Steps 4–6 (scaffold) require empty_exam.pdf; if it is missing the pipeline raises at step 4.
Steps 10–14 require the scaffold produced by steps 4–6.
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
    from xscore.marking.parse_instruction import parse_prompt
    from xscore.marking.timing_report import write_timing_report
    from xscore.preprocessing.start_scan import (
        CLEANED_SCAN_PDF,
        autorotate_phase,
        deskew_phase,
        detect_blank_pages_phase,
        find_source_scan_match,
    )
    from xscore.scaffold.generate_scaffold import build_scaffold
    from xscore.shared.exam_paths import (
        artifact_exam_student_list_json_path,
        artifact_exam_student_list_md_path,
    )
    from xscore.shared.load_student_list import read_student_list

    def _step(num: int, fn: Callable) -> any:
        """Call fn() wrapped with on_step events. Re-raises on failure."""
        t0 = time.perf_counter()
        on_step(num, "running", None)
        try:
            result = fn()
            on_step(num, "done", round(time.perf_counter() - t0, 2))
            return result
        except Exception:
            on_step(num, "failed", round(time.perf_counter() - t0, 2))
            raise

    # ---------------------------------------------------------------------- step 1
    effective_dpi = dpi or 400
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
            on_step(1, "failed", round(time.perf_counter() - t0_1, 2))
            logging.exception("Step 1 prompt parse failed")
            on_line(f"Step 1 — Parse failed ({exc}); using defaults.")
    else:
        t0_1 = time.perf_counter()
        on_step(1, "running", None)
        on_line("Step 1 — No prompt; using defaults.")
        on_step(1, "done", round(time.perf_counter() - t0_1, 2))

    # ---------------------------------------------------------------------- step 2
    # Folder is already known from upload — mark done immediately.
    on_step(2, "running", None)
    on_step(2, "done", 0.0)

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
    (artifact_dir / "meta").mkdir(parents=True, exist_ok=True)
    (artifact_dir / "meta" / "command.txt").write_text(" ".join(_cmd_parts), encoding="utf-8")

    # ---------------------------------------------------------------------- steps 4–6
    # empty_exam.pdf is required. Missing → step 4 raises with a clear message.
    empty_exam_path = folder / "empty_exam.pdf"
    scaffold = None
    step_timings: dict[str, float] = {}

    # Steps 4 and 5 timing are tracked via build_scaffold callbacks; step 6 is instant merge.
    t4_start: list[float] = []
    t5_start: list[float] = []

    def _on_exam_done(raw_questions: list) -> None:
        elapsed = round(time.perf_counter() - t4_start[0], 2) if t4_start else 0.0
        step_timings["step_4_s"] = elapsed
        on_step(4, "done", elapsed)
        on_line(f"Step 4 — {len(raw_questions)} top-level questions extracted.")
        on_step(5, "running", None)
        t5_start.append(time.perf_counter())

    def _on_scheme_done(scheme_questions: list) -> None:
        elapsed = round(time.perf_counter() - t5_start[0], 2) if t5_start else 0.0
        step_timings["step_5_s"] = elapsed
        on_step(5, "done", elapsed)
        on_line(f"Step 5 — {len(scheme_questions)} answers in mark scheme.")
        on_step(6, "running", None)

    def _build() -> any:
        t4_start.append(time.perf_counter())
        on_step(4, "running", None)
        on_line("Step 4 — Parsing exam PDF (AI)…")
        if not empty_exam_path.is_file():
            raise FileNotFoundError(
                "empty_exam.pdf is required but was not uploaded. "
                "Please re-submit with the blank exam PDF attached."
            )
        result = build_scaffold(
            folder,
            artifact_dir=artifact_dir,
            exam_pdf_override=empty_exam_path,
            on_exam_complete=_on_exam_done,
            on_scheme_complete=_on_scheme_done,
            students=students,
        )
        return result

    t6_0 = time.perf_counter()
    try:
        scaffold = _build()
        elapsed_6 = round(time.perf_counter() - t6_0, 2)
        on_step(6, "done", elapsed_6)
        step_timings["step_6_s"] = elapsed_6
        on_line(
            f"Step 6 — {len(scaffold.gradable_questions)} gradable parts  ·  "
            f"{scaffold.total_marks} marks."
        )
    except Exception:
        if "step_4_s" not in step_timings:
            on_step(4, "failed", 0.0)
        if "step_5_s" not in step_timings:
            on_step(5, "failed", 0.0)
        if "step_6_s" not in step_timings:
            on_step(6, "failed", 0.0)
        raise

    # ---------------------------------------------------------------------- steps 7–9
    on_line("Step 7 — Detecting blank pages…")
    source_scan = find_source_scan_match(folder, artifact_dir, effective_dpi)

    def _blank() -> None:
        detect_blank_pages_phase(
            source_scan,
            artifact_dir,
            analysis_dpi=effective_dpi,
        )

    _step(7, _blank)
    on_line("Step 7 — Blank pages detected.")

    def _rotate() -> None:
        autorotate_phase(artifact_dir)

    on_line("Step 8 — Autorotating…")
    _step(8, _rotate)

    on_line("Step 9 — Deskewing…")

    def _deskew() -> Path:
        return deskew_phase(folder, artifact_dir, effective_dpi)

    cleaned_pdf: Path = _step(9, _deskew)
    on_line("Step 9 — Cleaned scan ready.")

    # ---------------------------------------------------------------------- step 10
    on_line("Step 10 — Computing geometry and assigning pages to students (AI)…")

    def _geometry() -> dict:
        geo = compute_geometry(cleaned_pdf, scaffold.page_count, students)
        write_geometry_artifacts(artifact_dir, geo)
        return geo

    t10 = time.perf_counter()
    on_step(10, "running", None)
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
        elapsed_10 = round(time.perf_counter() - t10, 2)
        step_timings["step_10_s"] = elapsed_10
        on_step(10, "done", elapsed_10)
        on_line(f"Step 10 — {len(page_assignments)} students detected from scan.")
    except Exception:
        on_step(10, "failed", round(time.perf_counter() - t10, 2))
        raise

    # ---------------------------------------------------------------------- step 11
    on_line("Step 11 — Building marking blueprints…")

    def _blueprints() -> list:
        bps = build_blueprints(scaffold, artifact_dir)
        on_line(f"Step 11 — {len(bps)} page blueprint(s) written.")
        return bps

    t11 = time.perf_counter()
    _step(11, _blueprints)
    step_timings["step_11_s"] = round(time.perf_counter() - t11, 2)

    # ---------------------------------------------------------------------- step 12
    on_line("Step 12 — AI marking…")
    ctx = SimpleNamespace(
        cleaned_pdf=cleaned_pdf,
        artifact_dir=artifact_dir,
        scaffold=scaffold,
        pages_per_student=pages_per_student,
        num_students=num_students,
    )

    def _mark() -> list:
        return run_ai_marking(ctx, dpi=effective_dpi)

    t12 = time.perf_counter()
    api_calls: list[dict] = _step(12, _mark)
    step_timings["step_12_s"] = round(time.perf_counter() - t12, 2)
    on_line(f"Step 12 — {len(api_calls)} API calls completed.")

    # ---------------------------------------------------------------------- step 13
    on_line("Step 13 — Compiling reports…")

    def _reports() -> list:
        summaries = compile_reports(ctx)
        on_line(f"Step 13 — {len(summaries)} student report(s) written.")
        return summaries

    t13 = time.perf_counter()
    _step(13, _reports)
    step_timings["step_13_s"] = round(time.perf_counter() - t13, 2)

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
