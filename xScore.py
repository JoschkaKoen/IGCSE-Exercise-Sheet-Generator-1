#!/usr/bin/env python3
"""
xScore.py
---------
Exam scan grading pipeline (steps 1–14) — run from the eXercise project root.

Steps:
  1. Parse the natural language prompt (via Kimi).
  2. Locate the exam folder.
  3. Read the student roster from StudentList.xlsx.
  4. Detect blank scan pages.
  5. Autorotate (remove blanks, apply /Rotate metadata).
  6. Deskew (small-angle per-half correction) → 3_cleaned_scan.pdf.
  7. Assign scan pages to students (name OCR via Kimi) → 10_exam_student_list.json.
  8. AI: parse exam PDF → question hierarchy + layout → 4_exam_questions.json + 4_exam_questions.md.
  9. AI: parse mark scheme → correct answers + criteria → 5_mark_scheme.json + 5_mark_scheme.md.
 10. Merge scaffold → 6_scaffold.json + 6_scaffold.md.
 11. Build per-page AI marking blueprints → 11_ai_marking_blueprint_N.json.
 12. AI: grade each student page (Kimi) → 12_marked_*.json.
 13. Merge per-page results into student and class reports → 13_student_report_*.json + PDF.
 14. Produce final graded summary.

Usage:
    python xScore.py "grade Space Physics Unit Test"
    python xScore.py "grade the exam" --folder "exams/space_physics" --dpi 300
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv

from xscore.shared.student_artifacts import write_student_artifacts

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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="xScore.py",
        description="Grade an exam scan (steps 1–14).",
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
    args = parser.parse_args()
    return args


# ---------------------------------------------------------------------------
# Context dataclass
# ---------------------------------------------------------------------------

@dataclass
class _Ctx:
    args: argparse.Namespace
    timestamp: str
    instruction: Any = None          # TaskInstruction
    parse_elapsed: float = 0.0
    force_clean_scan: bool = False
    folder: Path | None = None
    artifact_dir: Path | None = None
    students: list[str] | None = None
    scaffold: Any = None
    cleaned_pdf: Path | None = None
    pipeline_completed_ok: bool = False
    # Steps 10–14: AI marking pipeline
    num_students: int = 0
    pages_per_student: int = 0
    step_timings_marking: dict = None        # populated in steps 10–14
    marking_api_calls: list = None           # populated in step 12
    marking_failures: list = None            # populated in step 12 (pages that exhausted all retries)
    page_assignments: list | None = None     # list[PageAssignment] set by step 10

    def __post_init__(self) -> None:
        if self.step_timings_marking is None:
            self.step_timings_marking = {}
        if self.marking_api_calls is None:
            self.marking_api_calls = []
        if self.marking_failures is None:
            self.marking_failures = []


def _print_footer(ctx: _Ctx, gi: SimpleNamespace, elapsed: float) -> None:
    t = f"{elapsed:.1f}s"
    if ctx.pipeline_completed_ok:
        gi.info_line(f"Run · {t} · complete")
    else:
        gi.info_line(f"Run · {t}")
    gi.get_console().print()
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Lazy imports (after load_dotenv so env vars are available)
# ---------------------------------------------------------------------------

def _load_imports() -> SimpleNamespace:
    from xscore.marking.ai_mark import run_ai_marking
    from xscore.marking.assign_pages_to_students import (
        assign_pages,
        page_assignments_to_json,
        page_assignments_to_md,
    )
    from xscore.marking.blueprints import build_blueprints
    from xscore.marking.find_exam_folder import find_folder
    from xscore.marking.geometry import compute_geometry, write_geometry_artifacts
    from xscore.marking.merge_reports import compile_reports, load_student_results_from_reports
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
    from xscore.shared.load_ground_truth import evaluate_results, load_ground_truth
    from xscore.shared.load_student_list import read_student_list
    from xscore.shared.terminal_ui import (
        api_latency_line,
        err_line,
        format_duration,
        get_console,
        info_line,
        ok_line,
        pipeline_step,
        warn_line,
    )

    return SimpleNamespace(
        find_folder=find_folder,
        parse_prompt=parse_prompt,
        build_scaffold=build_scaffold,
        CLEANED_SCAN_PDF=CLEANED_SCAN_PDF,
        autorotate_phase=autorotate_phase,
        deskew_phase=deskew_phase,
        detect_blank_pages_phase=detect_blank_pages_phase,
        find_source_scan_match=find_source_scan_match,
        read_student_list=read_student_list,
        # Steps 10–14
        compute_geometry=compute_geometry,
        write_geometry_artifacts=write_geometry_artifacts,
        assign_pages=assign_pages,
        page_assignments_to_json=page_assignments_to_json,
        page_assignments_to_md=page_assignments_to_md,
        artifact_exam_student_list_json_path=artifact_exam_student_list_json_path,
        artifact_exam_student_list_md_path=artifact_exam_student_list_md_path,
        build_blueprints=build_blueprints,
        run_ai_marking=run_ai_marking,
        compile_reports=compile_reports,
        load_student_results_from_reports=load_student_results_from_reports,
        load_ground_truth=load_ground_truth,
        evaluate_results=evaluate_results,
        write_timing_report=write_timing_report,
        # Terminal UI
        api_latency_line=api_latency_line,
        err_line=err_line,
        format_duration=format_duration,
        get_console=get_console,
        info_line=info_line,
        ok_line=ok_line,
        pipeline_step=pipeline_step,
        warn_line=warn_line,
    )


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------

def _step01_parse(ctx: _Ctx, gi: SimpleNamespace) -> None:
    gi.pipeline_step(1, "AI API call — Parse grading instructions")
    t0 = time.perf_counter()
    ctx.instruction = gi.parse_prompt(ctx.args.prompt, dpi_override=ctx.args.dpi)
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
    gi.ok_line(
        f"{task_label}  ·  {scope}  ·  {inst.dpi} DPI  ·  "
        f"{gi.format_duration(ctx.parse_elapsed)}"
    )


def _step02_folder(ctx: _Ctx, gi: SimpleNamespace) -> None:
    assert ctx.instruction is not None
    gi.pipeline_step(2, "Select exam folder")
    ctx.folder = gi.find_folder(
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

    # Write step 1 summary now that artifact_dir exists (artifact_dir is created here, not in step 1)
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

    gi.ok_line(ctx.folder.name)

    from xscore.shared.exam_paths import validate_input_files
    validate_input_files(ctx.folder)


def _step03_students(ctx: _Ctx, gi: SimpleNamespace, *, on_header_printed=None, on_complete=None) -> None:
    assert ctx.folder is not None and ctx.artifact_dir is not None
    gi.pipeline_step(3, "Read student list")
    if on_header_printed is not None:
        on_header_printed()
    ctx.students = gi.read_student_list(ctx.folder, ctx.artifact_dir)
    gi.ok_line(f"{len(ctx.students)} students on the roster")
    write_student_artifacts(ctx.artifact_dir, ctx.students)
    if on_complete is not None:
        on_complete()


def _step08_09_10_scaffold(
    ctx: _Ctx,
    gi: SimpleNamespace,
    *,
    gate_event: "threading.Event | None" = None,
    background: bool = False,
) -> None:
    """Steps 8 (parse exam PDF), 9 (parse mark scheme), and 10 (merge scaffold)."""
    assert ctx.folder is not None and ctx.artifact_dir is not None
    t0 = time.perf_counter()
    gi.pipeline_step(
        8, "AI API call — Parse exam PDF",
        subtitle="running in background" if background else None,
    )

    def _on_exam_done(raw_questions: list) -> None:
        gi.ok_line(f"{len(raw_questions)} top-level questions extracted")
        if gate_event is not None:
            gate_event.wait()       # wait for step 7 before printing step 9 header
        gi.pipeline_step(
            9, "AI API call — Parse mark scheme",
            subtitle="completed in background" if background else None,
        )

    def _on_scheme_done(scheme_questions: list) -> None:
        gi.ok_line(f"{len(scheme_questions)} answers in mark scheme")
        gi.pipeline_step(10, "Create report")

    try:
        ctx.scaffold = gi.build_scaffold(
            ctx.folder,
            artifact_dir=ctx.artifact_dir,
            on_exam_complete=_on_exam_done,
            on_scheme_complete=_on_scheme_done,
            students=ctx.students,
        )
        qs = ctx.scaffold.gradable_questions
        gi.ok_line(
            f"{len(qs)} gradable parts  ·  {ctx.scaffold.total_marks} marks total"
            f"  ·  {gi.format_duration(time.perf_counter() - t0)}"
        )
    except FileNotFoundError as exc:
        gi.warn_line(f"No exam PDF found — scaffold skipped ({exc})")


def _scan_phases(ctx: _Ctx, gi: SimpleNamespace) -> None:
    """Steps 4–6: blank detection → autorotate → deskew."""
    assert ctx.folder is not None and ctx.artifact_dir is not None and ctx.instruction is not None
    ad = ctx.artifact_dir
    dpi = ctx.instruction.dpi

    match = gi.find_source_scan_match(ctx.folder, ad, dpi)

    from xscore.config import ROTATION_ANALYSIS_DPI
    gi.pipeline_step(4, "Detect blank pages")
    t0_7 = time.perf_counter()
    gi.detect_blank_pages_phase(match, ad, analysis_dpi=ROTATION_ANALYSIS_DPI, force_clean_scan=ctx.force_clean_scan)
    (ad / "meta" / "4_blank_detection_summary.json").write_text(
        json.dumps({"step": 4, "elapsed_s": round(time.perf_counter() - t0_7, 3), "status": "ok"}, indent=2),
        encoding="utf-8",
    )

    gi.pipeline_step(5, "Autorotate")
    t0_rot = time.perf_counter()
    gi.autorotate_phase(ad)
    elapsed_rot = time.perf_counter() - t0_rot
    gi.info_line(gi.format_duration(elapsed_rot))
    (ad / "meta" / "5_autorotate_summary.json").write_text(
        json.dumps({"step": 5, "elapsed_s": round(elapsed_rot, 3), "status": "ok"}, indent=2),
        encoding="utf-8",
    )

    gi.pipeline_step(6, "Deskew")
    t0_9 = time.perf_counter()
    ctx.cleaned_pdf = gi.deskew_phase(ctx.folder, ad, dpi)
    (ad / "meta" / "6_deskew_summary.json").write_text(
        json.dumps({"step": 6, "elapsed_s": round(time.perf_counter() - t0_9, 3), "status": "ok"}, indent=2),
        encoding="utf-8",
    )


def _run_step3_and_scan_parallel(ctx: _Ctx, gi: SimpleNamespace, *, on_students_ready=None) -> None:
    """Step 3 runs on the main thread; scan phases (4–6) run concurrently.

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
            _scan_phases(ctx, gi)
        except BaseException as exc:
            scan_exc = exc

    step3_exc: BaseException | None = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_scan_wrapper)
        try:
            _step03_students(ctx, gi,
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


def _run_steps3to10_parallel(ctx: _Ctx, gi: SimpleNamespace) -> None:
    """Steps 3–10 with maximum parallelism after step 2.

    Main thread : step 3 → (steps 4-6 background) → step 7
    Scaffold thread: (wait students_ready) → steps 8-9-10
                     step 8 header fires immediately; steps 9/10 headers are
                     gated until step 7 finishes to keep output in logical order.
    Exceptions are re-raised in pipeline order after both threads finish.
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor

    _students_ready = threading.Event()
    _step7_done = threading.Event()
    scaffold_exc: BaseException | None = None
    main_exc: BaseException | None = None

    def _scaffold_wrapper() -> None:
        nonlocal scaffold_exc
        _students_ready.wait()
        try:
            _step08_09_10_scaffold(ctx, gi, gate_event=_step7_done, background=True)
        except BaseException as exc:
            scaffold_exc = exc

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(_scaffold_wrapper)
        try:
            _run_step3_and_scan_parallel(ctx, gi, on_students_ready=_students_ready.set)
            if ctx.cleaned_pdf:
                _step07_geometry(ctx, gi)
        except BaseException as exc:
            _students_ready.set()   # unblock scaffold thread on main-thread error
            main_exc = exc
        finally:
            _step7_done.set()       # always unblock scaffold gate (even on error/no scan)
        # exiting the `with` block waits for the scaffold thread to finish

    if main_exc is not None:
        raise main_exc
    if scaffold_exc is not None:
        raise scaffold_exc


# ---------------------------------------------------------------------------
# Marking pipeline steps (7–14)
# ---------------------------------------------------------------------------

def _exam_pdf_page_count(folder: Path) -> int:
    """Count pages in the exam PDF without building the scaffold."""
    from xscore.scaffold.generate_scaffold import find_exam_pdf
    import fitz
    with fitz.open(str(find_exam_pdf(folder))) as doc:
        return doc.page_count


def _step07_geometry(ctx: _Ctx, gi: SimpleNamespace) -> None:
    """Step 7 — Count scan/exam pages, derive student count."""
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    gi.pipeline_step(7, "Exam geometry")
    t0 = time.perf_counter()
    exam_pages = ctx.scaffold.page_count if ctx.scaffold else _exam_pdf_page_count(ctx.folder)
    geo = gi.compute_geometry(ctx.cleaned_pdf, exam_pages, ctx.students or [])
    gi.write_geometry_artifacts(ctx.artifact_dir, geo)
    ctx.num_students = geo["num_students"]
    ctx.pages_per_student = geo["pages_per_student"]
    if geo["roster_mismatch"]:
        gi.ok_line(
            f"Roster has {geo['num_students_roster']} students "
            f"but scan implies {geo['num_students']}"
        )
    gi.ok_line(
        f"{ctx.num_students} students  ·  {ctx.pages_per_student} pages each  "
        f"·  {geo['scan_pages']} scan pages total"
    )

    # --- Name detection sub-step ---
    gi.info_line("Detecting student names from scan pages …")
    t1 = time.perf_counter()
    ctx.page_assignments = gi.assign_pages(
        ctx.cleaned_pdf,
        ctx.students or [],
        pages_per_student=ctx.pages_per_student,
        artifact_dir=ctx.artifact_dir,
    )
    json_path = gi.artifact_exam_student_list_json_path(ctx.artifact_dir)
    json_path.write_text(
        gi.page_assignments_to_json(ctx.page_assignments), encoding="utf-8"
    )
    md_path = gi.artifact_exam_student_list_md_path(ctx.artifact_dir)
    md_path.write_text(
        gi.page_assignments_to_md(ctx.page_assignments), encoding="utf-8"
    )
    detected = len(ctx.page_assignments)
    gi.ok_line(
        f"{detected} students detected from scan  ·  "
        f"{gi.format_duration(time.perf_counter() - t1)}"
    )
    if detected != ctx.num_students:
        gi.warn_line(
            f"Name detection found {detected} students; geometry expected {ctx.num_students}. "
            "Step 12 will use the scan-detected list."
        )

    ctx.step_timings_marking["step_07_s"] = time.perf_counter() - t0


def _step11_blueprints(ctx: _Ctx, gi: SimpleNamespace) -> None:
    """Step 11 — Build per-page AI marking blueprints (no AI calls)."""
    assert ctx.scaffold is not None and ctx.artifact_dir is not None
    gi.pipeline_step(11, "AI marking blueprints")
    t0 = time.perf_counter()
    blueprints = gi.build_blueprints(ctx.scaffold, ctx.artifact_dir)
    gi.ok_line(f"{len(blueprints)} page blueprint(s) written")
    ctx.step_timings_marking["step_11_s"] = time.perf_counter() - t0


def _step12_mark(ctx: _Ctx, gi: SimpleNamespace) -> None:
    """Step 12 — AI marking: vision calls to fill blueprints for each student page."""
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    gi.pipeline_step(12, "AI marking")
    t0 = time.perf_counter()
    ctx.marking_api_calls = gi.run_ai_marking(ctx, dpi=ctx.instruction.dpi)
    gi.ok_line(
        f"{len(ctx.marking_api_calls)} API calls  ·  "
        f"{ctx.num_students * ctx.pages_per_student} pages marked"
    )
    ctx.step_timings_marking["step_12_s"] = time.perf_counter() - t0


def _step13_reports(ctx: _Ctx, gi: SimpleNamespace) -> None:
    """Step 13 — Merge per-page results into student + class reports; compile PDFs."""
    assert ctx.scaffold is not None and ctx.artifact_dir is not None
    gi.pipeline_step(13, "Compile reports")
    t0 = time.perf_counter()
    summaries = gi.compile_reports(ctx)
    _known = [s["percentage"] for s in summaries if s["percentage"] is not None]
    _avg_str = f"{round(sum(_known) / len(_known), 1)}%" if _known else "N/A"
    gi.ok_line(f"{len(summaries)} student report(s)  ·  class avg {_avg_str}")
    ctx.step_timings_marking["step_13_s"] = time.perf_counter() - t0


def _step14_timing(ctx: _Ctx, gi: SimpleNamespace) -> None:
    """Step 14 — Write timing summary (14_timing.json / .md) and accuracy report."""
    assert ctx.artifact_dir is not None
    gi.pipeline_step(14, "Timing summary")
    t0 = time.perf_counter()

    accuracy_summary = None
    if ctx.folder is not None:
        ground_truth = gi.load_ground_truth(ctx.folder, ctx.scaffold)
        if ground_truth and ctx.scaffold:
            student_results = gi.load_student_results_from_reports(ctx.artifact_dir)
            accuracy_summary = gi.evaluate_results(student_results, ground_truth, ctx.scaffold)
            from xscore.shared.exam_paths import artifact_accuracy_json_path
            artifact_accuracy_json_path(ctx.artifact_dir).write_text(
                json.dumps(accuracy_summary, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            gi.info_line(
                f"Accuracy: {accuracy_summary['overall_correct']}/"
                f"{accuracy_summary['overall_total']} "
                f"({accuracy_summary['overall_accuracy_pct']:.1f}%)"
            )

    ctx.step_timings_marking["step_14_s"] = round(time.perf_counter() - t0, 3)
    gi.write_timing_report(
        ctx.artifact_dir,
        ctx.step_timings_marking,
        ctx.marking_api_calls,
        accuracy_summary=accuracy_summary,
        failures=ctx.marking_failures,
    )


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def _run(args: argparse.Namespace, timestamp: str) -> None:
    gi = _load_imports()
    ctx = _Ctx(args=args, timestamp=timestamp)
    t0 = time.perf_counter()
    try:
        _step01_parse(ctx, gi)
        _step02_folder(ctx, gi)
        _run_steps3to10_parallel(ctx, gi)
        if ctx.cleaned_pdf and ctx.scaffold:
            _step11_blueprints(ctx, gi)
            _step12_mark(ctx, gi)
            _step13_reports(ctx, gi)
            _step14_timing(ctx, gi)
        gi.ok_line("Pipeline complete.")
        ctx.pipeline_completed_ok = True
        if ctx.cleaned_pdf:
            gi.info_line(f"Cleaned scan: {ctx.cleaned_pdf}")
    finally:
        _print_footer(ctx, gi, time.perf_counter() - t0)


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
