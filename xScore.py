#!/usr/bin/env python3
"""
xScore.py
---------
Exam scan grading pipeline (steps 1–16) — run from the eXercise project root.

Steps:
  1. Parse the natural language prompt (via Kimi).
  2. Locate the exam folder.
  3. Read the student roster from StudentList.xlsx.
  4. Merge duplex scan halves into one PDF (only when two scan files are found).
  5. Detect blank scan pages.
  6. Autorotate (remove blanks, apply /Rotate metadata).
  7. Deskew (small-angle per-half correction) → 7_cleaned_scan.pdf.
  8. Assign scan pages to students (name OCR) → 8_exam_student_list.json.
  9. AI: detect exam layout + split multi-up PDFs → 9_exam_layout.json (split mode only).
 10. AI: parse exam PDF → question hierarchy → 10_exam_questions.json + 10_exam_questions.md.
 11. AI: parse mark scheme → correct answers + criteria → 11_mark_scheme.json + 11_mark_scheme.md.
 12. Merge scaffold → 12_report.json + 12_report.md.
 13. Build per-page AI marking blueprints → 13_ai_marking_blueprint_N.json.
 14. AI: grade each student page → 14_marked_*.json.
 15. Merge per-page results into student and class reports → 15_student_report_*.json + PDF.
 16. Timing summary.

Usage:
    python xScore.py "grade Space Physics Unit Test"
    python xScore.py "grade the exam" --folder "exams/space_physics" --dpi 300
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from xscore.shared.models import ExamScaffold, TaskInstruction

__version__ = "0.2"

class _Tee:
    """Duplicate stdout to a log file, stripping ANSI colour codes from the file."""

    def __init__(self, log_path: Path, *, argv: list[str] | None = None) -> None:
        self._stdout = sys.stdout
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = log_path.open("w", encoding="utf-8")
        cmd = shlex.join(argv if argv is not None else sys.argv)
        self._log.write(f"Command: {cmd}\n\n")
        self._log.flush()

    def write(self, text: str) -> int:
        self._stdout.write(text)
        plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        plain = re.sub(r"\x1b\][^\x07]*\x07", "", plain)
        self._log.write(plain)
        return len(text)

    def flush(self) -> None:
        self._stdout.flush()
        self._log.flush()

    def isatty(self) -> bool:
        return self._stdout.isatty()

    def close(self) -> None:
        sys.stdout = self._stdout
        self._log.close()


class _EarlyExit(Exception):
    """Pipeline stopped because --stop-after N was reached."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="xScore.py",
        description="Grade an exam scan (steps 1–16).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "prompt",
        help='Grading instruction, e.g. "grade Space Physics Unit Test"',
    )
    parser.add_argument(
        "--folder",
        default=None,
        metavar="PATH",
        help="Exam folder path (overrides folder hint from prompt)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=None,
        metavar="N",
        help="Rendering DPI (overrides dpi from prompt; default 400)",
    )
    parser.add_argument(
        "--force-clean-scan",
        action="store_true",
        default=False,
        help="Rebuild cleaned scan even if cached",
    )
    parser.add_argument(
        "--stop-after",
        type=int,
        default=None,
        metavar="N",
        help="Stop pipeline after step N completes (e.g. 7 to stop after geometry)",
    )
    args = parser.parse_args()
    return args


# ---------------------------------------------------------------------------
# Context dataclass
# ---------------------------------------------------------------------------

@dataclass
class _Ctx:
    args: argparse.Namespace
    timestamp: str
    instruction: "TaskInstruction | None" = None
    parse_elapsed: float = 0.0
    force_clean_scan: bool = False
    folder: Path | None = None
    artifact_dir: Path | None = None
    students: list[str] | None = None
    scaffold: "ExamScaffold | None" = None
    cleaned_pdf: Path | None = None
    pipeline_completed_ok: bool = False
    # Steps 12–16: AI marking pipeline
    num_students: int = 0
    pages_per_student: int = 0
    step_timings_marking: dict[str, float] = field(default_factory=dict)
    marking_api_calls: list[dict] = field(default_factory=list)
    marking_failures: list[dict] = field(default_factory=list)
    page_assignments: list | None = None     # list[PageAssignment] set by step 8
    cover_page_mode: bool = False            # True when step 8 detects cover pages in the scan
    step_offset: int = 0                     # 1 when split-subpages mode adds step 9 (layout + cut)
    stop_after: int = 9999                   # --stop-after N; 9999 = run everything

    def __post_init__(self) -> None:
        if getattr(self.args, "stop_after", None) is not None:
            self.stop_after = self.args.stop_after


# ---------------------------------------------------------------------------
# Helpers that do not depend on deferred imports
# ---------------------------------------------------------------------------

def _exam_pdf_page_count(folder: Path) -> int:
    """Count pages in the exam PDF without building the scaffold."""
    from xscore.scaffold.generate_scaffold import find_exam_pdf
    import fitz
    with fitz.open(str(find_exam_pdf(folder))) as doc:
        return doc.page_count


# ---------------------------------------------------------------------------
# Main runner — all pipeline step functions are defined here so they share
# the deferred imports via closure, avoiding a SimpleNamespace intermediary.
# ---------------------------------------------------------------------------

def _run(args: argparse.Namespace, timestamp: str) -> None:
    # Deferred imports: all heavy modules and anything that reads env vars at
    # import time (e.g. xscore/config.py) must be imported AFTER load_dotenv().
    from xscore.marking.ai_mark import run_ai_marking
    from xscore.marking.assign_pages_to_students import (
        assign_pages,
        page_assignments_to_json,
        page_assignments_to_md,
    )
    from xscore.marking.blueprints import build_blueprints
    from xscore.marking.find_exam_folder import find_folder, validate_input_files
    from xscore.marking.geometry import compute_geometry, write_geometry_artifacts
    from xscore.marking.merge_reports import compile_reports, load_student_results_from_reports
    from xscore.marking.parse_instruction import parse_prompt
    from xscore.marking.timing_report import write_timing_report
    from xscore.preprocessing.start_scan import (
        autorotate_phase,
        deskew_phase,
        detect_blank_pages_phase,
        find_source_scan_match,
        find_two_scan_pdfs,
        merge_duplex_scans_phase,
    )
    from xscore.scaffold.generate_scaffold import build_scaffold
    from xscore.shared.exam_paths import (
        artifact_exam_student_list_json_path,
        artifact_exam_student_list_md_path,
    )
    from xscore.shared.load_ground_truth import evaluate_results, load_ground_truth
    from xscore.shared.load_student_list import read_student_list
    from xscore.shared.student_artifacts import write_student_artifacts
    from xscore.shared.terminal_ui import (
        format_duration,
        get_console,
        info_line,
        ok_line,
        pipeline_step,
        warn_line,
    )

    # -----------------------------------------------------------------------
    # Pipeline step definitions
    # -----------------------------------------------------------------------

    def _step01_parse(ctx: _Ctx) -> None:
        pipeline_step(1, "AI API call — Parse grading instructions")
        t0 = time.perf_counter()
        ctx.instruction = parse_prompt(ctx.args.prompt, dpi_override=ctx.args.dpi)
        ctx.parse_elapsed = time.perf_counter() - t0
        assert ctx.instruction is not None
        inst = ctx.instruction

        ctx.force_clean_scan = ctx.args.force_clean_scan or inst.force_clean_scan

        task_labels = {
            "check_answers": "Grade answers",
            "check_mc": "Multiple choice only",
            "count_marks": "Count marks",
            "build_scaffold": "Build structure",
            "clean_scan": "Clean scan",
        }
        task_label = task_labels.get(inst.task_type, inst.task_type.replace("_", " ").strip())
        sf = inst.student_filter
        if sf.mode == "all":
            scope = "all students"
        elif sf.mode == "first_n" and sf.n > 0:
            scope = f"first {sf.n} students"
        elif sf.names:
            scope = f"{len(sf.names)} named students"
        else:
            scope = sf.mode.replace("_", " ")
        ok_line(
            f"{task_label}  ·  {scope}  ·  {inst.dpi} DPI  ·  "
            f"{format_duration(ctx.parse_elapsed)}"
        )

    def _step02_folder(ctx: _Ctx) -> None:
        assert ctx.instruction is not None
        pipeline_step(2, "Select exam folder")
        ctx.folder = find_folder(
            instruction_hint=ctx.instruction.folder_hint,
            cli_override=ctx.args.folder,
            ai_folder_path=None if ctx.args.folder else ctx.instruction.folder_path,
        )
        assert ctx.folder is not None
        stem = ctx.folder.name.replace(" ", "_")
        exam_output_root = Path("output") / "xscore" / stem
        exam_output_root.mkdir(parents=True, exist_ok=True)
        ctx.artifact_dir = exam_output_root / ctx.timestamp
        suffix = 1
        while ctx.artifact_dir.exists():
            suffix += 1
            ctx.artifact_dir = exam_output_root / f"{ctx.timestamp}_{suffix}"
        ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
        meta_dir = ctx.artifact_dir / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / "command.txt").write_text(shlex.join(sys.argv), encoding="utf-8")

        # Write step 1 summary now that artifact_dir exists (created here, not in step 1)
        inst = ctx.instruction
        step1_summary = {
            "step": 1,
            "elapsed_s": round(ctx.parse_elapsed, 3),
            "task_type": inst.task_type,
            "dpi": inst.dpi,
            "status": "ok",
        }
        (meta_dir / "1_parse_summary.json").write_text(
            json.dumps(step1_summary, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        ok_line(ctx.folder.name)
        validate_input_files(ctx.folder)

    def _step03_students(ctx: _Ctx, *, on_header_printed=None, on_complete=None) -> None:
        assert ctx.folder is not None and ctx.artifact_dir is not None
        pipeline_step(3, "Read student list")
        if on_header_printed is not None:
            on_header_printed()
        ctx.students = read_student_list(ctx.folder, ctx.artifact_dir)
        ok_line(f"{len(ctx.students)} students on the roster")
        write_student_artifacts(ctx.artifact_dir, ctx.students)
        if on_complete is not None:
            on_complete()

    def _scaffold_steps(ctx: _Ctx, *, background: bool = False) -> None:
        """Steps 9–12 in split mode (9 layout+cut, 10 exam, 11 scheme, 12 merge)
        or 9–11 in legacy mode (9 exam, 10 scheme, 11 merge)."""
        import os as _os
        assert ctx.folder is not None and ctx.artifact_dir is not None
        t0 = time.perf_counter()

        _split = _os.getenv("READ_EXAM_PDF_SPLIT", "1").strip() not in ("0", "false", "no")
        ctx.step_offset = 1 if _split else 0
        off = ctx.step_offset

        if _split:
            pipeline_step(
                9, "Detect exam layout",
                subtitle="running in background" if background else None,
            )
        else:
            pipeline_step(
                9, "Parse exam PDF",
                subtitle="running in background" if background else None,
            )

        def _on_cut_done(skipped: bool) -> None:
            pipeline_step(
                10, "Parse exam PDF",
                subtitle="running in background" if background else None,
            )

        def _on_exam_done(raw_questions: list) -> None:
            ok_line(f"{len(raw_questions)} top-level questions extracted")
            pipeline_step(
                10 + off, "Parse mark scheme",
                subtitle="completed in background" if background else None,
            )

        def _on_scheme_done(scheme_questions: list) -> None:
            ok_line(f"{len(scheme_questions)} answers in mark scheme")
            pipeline_step(11 + off, "Create report")

        try:
            ctx.scaffold = build_scaffold(
                ctx.folder,
                artifact_dir=ctx.artifact_dir,
                force_rebuild=True,
                on_layout_complete=None,
                on_cut_complete=_on_cut_done if _split else None,
                on_exam_complete=_on_exam_done,
                on_scheme_complete=_on_scheme_done,
                students=ctx.students,
            )
            qs = ctx.scaffold.gradable_questions
            ok_line(
                f"{len(qs)} gradable parts  ·  {ctx.scaffold.total_marks} marks total"
                f"  ·  {format_duration(time.perf_counter() - t0)}"
            )
        except FileNotFoundError as exc:
            warn_line(f"No exam PDF found — scaffold skipped ({exc})")

    def _scan_phases(ctx: _Ctx) -> None:
        """Steps 4–7: optional duplex merge → blank detection → autorotate → deskew."""
        assert ctx.folder is not None and ctx.artifact_dir is not None and ctx.instruction is not None
        ad = ctx.artifact_dir
        dpi = ctx.instruction.dpi

        two = find_two_scan_pdfs(ctx.folder, ad)
        if two is not None:
            pipeline_step(4, "Merge duplex scans")
            match = merge_duplex_scans_phase(two[0], two[1], ad, force_rebuild=ctx.force_clean_scan)
        else:
            match = find_source_scan_match(ctx.folder, ad, dpi)

        from xscore.config import ROTATION_ANALYSIS_DPI
        pipeline_step(5, "Detect blank pages")
        t0_7 = time.perf_counter()
        detect_blank_pages_phase(match, ad, analysis_dpi=ROTATION_ANALYSIS_DPI, force_clean_scan=ctx.force_clean_scan)
        (ad / "5_blank_detection_summary.json").write_text(
            json.dumps({"step": 5, "elapsed_s": round(time.perf_counter() - t0_7, 3), "status": "ok"}, indent=2),
            encoding="utf-8",
        )

        pipeline_step(6, "Autorotate")
        t0_rot = time.perf_counter()
        autorotate_phase(ad)
        elapsed_rot = time.perf_counter() - t0_rot
        info_line(format_duration(elapsed_rot))
        (ad / "6_autorotate_summary.json").write_text(
            json.dumps({"step": 6, "elapsed_s": round(elapsed_rot, 3), "status": "ok"}, indent=2),
            encoding="utf-8",
        )

        pipeline_step(7, "Deskew")
        t0_9 = time.perf_counter()
        ctx.cleaned_pdf = deskew_phase(ctx.folder, ad, dpi)
        (ad / "7_deskew_summary.json").write_text(
            json.dumps({"step": 7, "elapsed_s": round(time.perf_counter() - t0_9, 3), "status": "ok"}, indent=2),
            encoding="utf-8",
        )

    def _run_step3_and_scan_parallel(ctx: _Ctx, *, on_students_ready=None) -> None:
        """Step 3 runs on the main thread; scan phases (4–7) run concurrently.

        A threading.Event gates the scan thread so the step 4 header cannot print
        before the step 3 header, keeping terminal output in step order.
        Exceptions are caught and re-raised in pipeline order after both finish.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        _scan_ready = threading.Event()
        scan_exc: BaseException | None = None

        def _scan_wrapper() -> None:
            nonlocal scan_exc
            _scan_ready.wait()          # wait for step 3 header before printing step 4
            try:
                _scan_phases(ctx)
            except BaseException as exc:
                scan_exc = exc

        run_scan = ctx.stop_after >= 4
        step3_exc: BaseException | None = None
        with ThreadPoolExecutor(max_workers=1) as pool:
            if run_scan:
                pool.submit(_scan_wrapper)
            try:
                _step03_students(ctx,
                                 on_header_printed=_scan_ready.set,
                                 on_complete=on_students_ready)
            except BaseException as exc:
                _scan_ready.set()       # unblock scan thread even on step-3 error
                if on_students_ready is not None:
                    on_students_ready()  # unblock scaffold thread even on step-3 error
                step3_exc = exc
            # exiting the `with` block waits for the scan thread to finish

        if step3_exc is not None:
            raise step3_exc
        if scan_exc is not None:
            raise scan_exc

    def _run_steps3to11_sequential(ctx: _Ctx) -> None:
        """Steps 3–11 run one after the other in the main thread."""
        _step03_students(ctx)
        if ctx.stop_after <= 3: raise _EarlyExit()
        _scan_phases(ctx)
        if ctx.stop_after <= 7: raise _EarlyExit()
        if ctx.cleaned_pdf:
            _step08_geometry(ctx)
        if ctx.stop_after <= 8: raise _EarlyExit()
        _scaffold_steps(ctx)

    def _run_steps3to11_parallel(ctx: _Ctx) -> None:
        """Steps 3–11 with maximum parallelism after step 2.

        Main thread : step 3 → (steps 4-7 background) → step 8
        Scaffold thread: (wait students_ready) → (wait step8_done) → steps 9-10-11
                         All scaffold headers are gated until step 8 finishes to
                         keep output in logical order.
        Exceptions are re-raised in pipeline order after both threads finish.
        """
        import threading
        from concurrent.futures import ThreadPoolExecutor

        _students_ready = threading.Event()
        _step8_done = threading.Event()
        scaffold_exc: BaseException | None = None
        main_exc: BaseException | None = None

        def _scaffold_wrapper() -> None:
            nonlocal scaffold_exc
            _students_ready.wait()
            _step8_done.wait()  # wait for step 8 before printing any scaffold headers
            try:
                _scaffold_steps(ctx, background=True)
            except BaseException as exc:
                scaffold_exc = exc

        run_scaffold = ctx.stop_after >= 9
        with ThreadPoolExecutor(max_workers=1) as pool:
            if run_scaffold:
                pool.submit(_scaffold_wrapper)
            try:
                _run_step3_and_scan_parallel(ctx, on_students_ready=_students_ready.set)
                if ctx.stop_after <= 7:
                    raise _EarlyExit()
                if ctx.cleaned_pdf:
                    _step08_geometry(ctx)
            except BaseException as exc:
                _students_ready.set()   # unblock scaffold thread on main-thread error
                main_exc = exc
            finally:
                _step8_done.set()       # always unblock scaffold gate (even on error/no scan)
            # exiting the `with` block waits for the scaffold thread to finish

        if main_exc is not None:
            raise main_exc
        if scaffold_exc is not None:
            raise scaffold_exc

    def _step08_geometry(ctx: _Ctx) -> None:
        """Step 8 — Count scan/exam pages, derive student count, detect cover pages."""
        assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
        pipeline_step(8, "Exam geometry")
        t0 = time.perf_counter()
        exam_pages = ctx.scaffold.page_count if ctx.scaffold else _exam_pdf_page_count(ctx.folder)
        geo = compute_geometry(ctx.cleaned_pdf, exam_pages, ctx.students or [])
        ctx.num_students = geo["num_students"]
        ctx.pages_per_student = geo["pages_per_student"]
        if geo["roster_mismatch"]:
            ok_line(
                f"Roster has {geo['num_students_roster']} students "
                f"but scan implies {geo['num_students']}"
            )
        ok_line(
            f"{ctx.num_students} students  ·  {ctx.pages_per_student} pages each  "
            f"·  {geo['scan_pages']} scan pages total"
        )
        # Write geometry artifacts immediately so downstream steps can read them even
        # if assign_pages() later raises (cover_page_mode will be overwritten below).
        geo["cover_page_mode"] = False
        write_geometry_artifacts(ctx.artifact_dir, geo)

        # --- Informational empty-exam cover check (does NOT determine behaviour) ---
        _empty_exam_has_cover: bool | None = None   # None = check was not performed
        try:
            import os as _os
            from xscore.scaffold.generate_scaffold import find_exam_pdf
            from google import genai as gai
            from eXercise.ai_client import parse_model_effort
            from xscore.marking.assign_pages_to_students import check_cover_page_text
            from xscore.shared.exam_paths import artifact_prompt_path
            _exam_pdf = find_exam_pdf(ctx.folder)
            _ec_api_key = (_os.environ.get("GEMINI_API_KEY", "") or _os.environ.get("GOOGLE_API_KEY", "")).strip()
            if _ec_api_key:
                _gai_client_ec = gai.Client(api_key=_ec_api_key)
                _ec_model, _ec_effort = parse_model_effort(_os.environ.get("EMPTY_EXAM_COVER_MODEL", "gemini-2.5-flash"))
                _ec_save = artifact_prompt_path(ctx.artifact_dir, "8_cover_empty_exam")
                _t_ec = time.perf_counter()
                _empty_exam_has_cover = check_cover_page_text(
                    _exam_pdf, 0, _gai_client_ec, _ec_model,
                    prompt_save_path=_ec_save,
                    effort=_ec_effort,
                )
                info_line(
                    f"Empty exam page 1: {'cover page' if _empty_exam_has_cover else 'no cover page'}  ·  {format_duration(time.perf_counter() - _t_ec)}"
                )
        except Exception as _e:
            warn_line(f"Empty exam cover check skipped: {_e}")

        # --- Name detection + cover-page detection (scan is authoritative) ---
        t1 = time.perf_counter()
        ctx.page_assignments = assign_pages(
            ctx.cleaned_pdf,
            ctx.students or [],
            pages_per_student=ctx.pages_per_student,
            artifact_dir=ctx.artifact_dir,
        )

        # Authoritative cover_page_mode: derived from scan result inside assign_pages()
        ctx.cover_page_mode = any(
            a.cover_page_number is not None for a in ctx.page_assignments
        )



        # Overwrite geometry artifacts with the authoritative cover_page_mode from the scan.
        geo["cover_page_mode"] = ctx.cover_page_mode
        write_geometry_artifacts(ctx.artifact_dir, geo)

        json_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
        json_path.write_text(
            page_assignments_to_json(ctx.page_assignments), encoding="utf-8"
        )
        md_path = artifact_exam_student_list_md_path(ctx.artifact_dir)
        md_path.write_text(
            page_assignments_to_md(ctx.page_assignments), encoding="utf-8"
        )
        detected = len(ctx.page_assignments)
        answer_pages = ctx.pages_per_student - (1 if ctx.cover_page_mode else 0)
        ok_line(
            f"{detected} students detected from scan  ·  {answer_pages} answer pages each"
            + ("  ·  cover page mode" if ctx.cover_page_mode else "")
            + f"  ·  {format_duration(time.perf_counter() - t1)}"
        )
        if detected != ctx.num_students:
            warn_line(
                f"Name detection found {detected} students; geometry expected {ctx.num_students}. "
                "Step 13 will use the scan-detected list."
            )

        ctx.step_timings_marking["assign_pages_s"] = time.perf_counter() - t0

    def _step12_blueprints(ctx: _Ctx) -> None:
        """Step 12/13 — Build per-page AI marking blueprints (no AI calls)."""
        assert ctx.scaffold is not None and ctx.artifact_dir is not None
        pipeline_step(12 + ctx.step_offset, "AI marking blueprints")
        t0 = time.perf_counter()
        blueprints = build_blueprints(ctx.scaffold, ctx.artifact_dir)
        ok_line(f"{len(blueprints)} page blueprint(s) written")
        ctx.step_timings_marking["blueprints_s"] = time.perf_counter() - t0

    def _step13_mark(ctx: _Ctx) -> None:
        """Step 13/14 — AI marking: vision calls to fill blueprints for each student page."""
        assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
        pipeline_step(13 + ctx.step_offset, "AI marking")
        t0 = time.perf_counter()
        ctx.marking_api_calls = run_ai_marking(ctx, dpi=ctx.instruction.dpi)
        ok_line(
            f"{len(ctx.marking_api_calls)} API calls  ·  "
            f"{ctx.num_students * ctx.pages_per_student} pages marked"
        )
        ctx.step_timings_marking["marking_s"] = time.perf_counter() - t0

    def _step14_reports(ctx: _Ctx) -> None:
        """Step 14/15 — Merge per-page results into student + class reports; compile PDFs."""
        assert ctx.scaffold is not None and ctx.artifact_dir is not None
        pipeline_step(14 + ctx.step_offset, "Compile reports")
        t0 = time.perf_counter()
        summaries = compile_reports(ctx)
        _known = [s["percentage"] for s in summaries if s["percentage"] is not None]
        _avg_str = f"{round(sum(_known) / len(_known), 1)}%" if _known else "N/A"
        ok_line(f"{len(summaries)} student report(s)  ·  class avg {_avg_str}")
        ctx.step_timings_marking["reports_s"] = time.perf_counter() - t0

    def _step15_timing(ctx: _Ctx) -> None:
        """Step 15/16 — Write timing summary (16_timing.json / .md) and accuracy report."""
        assert ctx.artifact_dir is not None
        pipeline_step(15 + ctx.step_offset, "Timing summary")
        t0 = time.perf_counter()

        accuracy_summary = None
        if ctx.folder is not None:
            ground_truth = load_ground_truth(ctx.folder, ctx.scaffold)
            if ground_truth and ctx.scaffold:
                student_results = load_student_results_from_reports(ctx.artifact_dir)
                accuracy_summary = evaluate_results(student_results, ground_truth, ctx.scaffold)
                from xscore.shared.exam_paths import artifact_accuracy_json_path
                artifact_accuracy_json_path(ctx.artifact_dir).write_text(
                    json.dumps(accuracy_summary, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                info_line(
                    f"Accuracy: {accuracy_summary['overall_correct']}/"
                    f"{accuracy_summary['overall_total']} "
                    f"({accuracy_summary['overall_accuracy_pct']:.1f}%)"
                )

        ctx.step_timings_marking["timing_s"] = round(time.perf_counter() - t0, 3)
        write_timing_report(
            ctx.artifact_dir,
            ctx.step_timings_marking,
            ctx.marking_api_calls,
            accuracy_summary=accuracy_summary,
            failures=ctx.marking_failures,
        )

    # -----------------------------------------------------------------------
    # Run the pipeline
    # -----------------------------------------------------------------------

    ctx = _Ctx(args=args, timestamp=timestamp)
    t0 = time.perf_counter()
    try:
        _step01_parse(ctx)
        if ctx.stop_after <= 1: raise _EarlyExit()
        _step02_folder(ctx)
        if ctx.stop_after <= 2: raise _EarlyExit()
        _run_steps3to11_sequential(ctx)
        if ctx.stop_after <= 11 + ctx.step_offset: raise _EarlyExit()
        if ctx.cleaned_pdf and ctx.scaffold:
            _step12_blueprints(ctx)
            if ctx.stop_after <= 12 + ctx.step_offset: raise _EarlyExit()
            _step13_mark(ctx)
            if ctx.stop_after <= 13 + ctx.step_offset: raise _EarlyExit()
            _step14_reports(ctx)
            if ctx.stop_after <= 14 + ctx.step_offset: raise _EarlyExit()
            _step15_timing(ctx)
        ok_line("Pipeline complete.")
        ctx.pipeline_completed_ok = True
        if ctx.cleaned_pdf:
            info_line(f"Cleaned scan: {ctx.cleaned_pdf}")
    except _EarlyExit:
        info_line(f"Stopped after step {ctx.stop_after}.")
    finally:
        t = f"{time.perf_counter() - t0:.1f}s"
        if ctx.pipeline_completed_ok:
            info_line(f"Run · {t} · complete")
        else:
            info_line(f"Run · {t}")
        get_console().print()
        sys.stdout.flush()


def main() -> None:
    load_dotenv("default.env")  # defaults (lower priority)
    load_dotenv()               # .env overrides (higher priority, secrets)
    args = parse_args()

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = Path("logs") / f"{timestamp}.log"
    tee = _Tee(log_path, argv=sys.argv)
    sys.stdout = tee
    from rich.rule import Rule
    from xscore.shared.terminal_ui import get_console, icon

    c = get_console()
    c.print()
    c.print(
        Rule(
            f"[bold blue]{icon('spark')}  xScore  —  {__version__}[/]",
            style="blue",
        )
    )
    try:
        _run(args, timestamp)
    finally:
        tee.flush()
        tee.close()


if __name__ == "__main__":
    main()
