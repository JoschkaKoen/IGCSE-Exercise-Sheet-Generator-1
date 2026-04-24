#!/usr/bin/env python3
"""
xScore.py
---------
Exam scan grading pipeline (steps 1–23) — run from the eXercise project root.

Steps:
  1. Parse the natural language prompt (via Kimi).
  2. Locate the exam folder.
  3. Read the student roster from StudentList.xlsx.
  4. Merge duplex scan halves into one PDF (only when two scan files are found).
  5. Detect blank scan pages.
  6. Autorotate (remove blanks, apply /Rotate metadata).
  7. Deskew (small-angle per-half correction) → 07_deskew/cleaned_scan.pdf.
  8. Scan geometry (page/student counts) → 08_exam_geometry/exam_geometry.json.
  9. Cover page detection (empty exam) → 09_cover_page/.
 10. AI: cover page detection (scan) → 10_cover_page_scan/.
 11. Student name detection (name OCR) → 11_student_names/exam_student_list.json.
 12. Page count validation.
 13. Page order check → 13_page_order/.
 14. Blank page detection → 14_blank_pages/.
 15. AI: detect exam layout → 15_detect_exam_layout/.
 16. Cut exam PDF (split multi-up pages) → 15_detect_exam_layout/split_exam.pdf (skipped for 1×1).
 17. AI: parse exam PDF → question hierarchy → 17_parse_exam_pdf/exam_questions.json.
 18. AI: parse mark scheme → correct answers + criteria → 18_parse_mark_scheme/mark_scheme.json.
 19. Merge scaffold → 19_create_report/report.json.
 20. Build per-page AI marking blueprints → 20_ai_marking_blueprints/.
 21. AI: grade each student page → 21_ai_marking/students/.
 22. Merge per-page results into student and class reports → 22_compile_reports/.
 23. Timing summary → 23_timing_summary/.
 24. AI Costs → 23_timing_summary/ (updates timing.json/md with token usage and cost breakdown).

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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import load_dotenv

from xscore.shared.pipeline_ctx import _Ctx, _EarlyExit

if TYPE_CHECKING:
    from xscore.shared.models import ExamScaffold, TaskInstruction

__version__ = "0.4"

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
        help="Stop pipeline after step N completes (e.g. 13 to stop after blank pages)",
    )
    parser.add_argument(
        "--from-step",
        type=int,
        default=None,
        metavar="N",
        help="Resume from step N using artifacts from a prior run (supported: blueprints, marking, reports step)",
    )
    parser.add_argument(
        "--resume-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="Prior artifact dir to resume from (auto-detects latest valid run if omitted)",
    )
    args = parser.parse_args()
    return args


# ---------------------------------------------------------------------------
# Input-file copy helper
# ---------------------------------------------------------------------------

def _copy_input_files(folder: Path, artifact_dir: Path) -> None:
    """Copy all input files used by this run into ``artifact_dir/input/``.

    Uses the same file-matching rules as :func:`validate_input_files` so every
    file that the pipeline reads is preserved alongside the artifacts.
    """
    import shutil
    from xscore.shared.exam_paths import artifact_input_dir
    dst = artifact_input_dir(artifact_dir)
    dst.mkdir(parents=True, exist_ok=True)
    _EXAM_SKIP = ("scan", "answer", "student", "cleaned")
    for f in folder.iterdir():
        if not f.is_file():
            continue
        # Match scan PDFs (same rule as validate_input_files)
        if f.suffix.lower() == ".pdf" and "scan" in f.name.lower() and "cleaned" not in f.name.lower():
            shutil.copy2(f, dst / f.name)
            continue
        # Match exam paper PDF (not a scan/answer/student file)
        if f.suffix.lower() == ".pdf" and not any(kw in f.name.lower() for kw in _EXAM_SKIP):
            shutil.copy2(f, dst / f.name)
            continue
        # Match mark scheme / answer PDF
        if f.suffix.lower() == ".pdf" and "answer" in f.name.lower():
            shutil.copy2(f, dst / f.name)
            continue
        # Match student roster (any name pattern)
        if any(kw in f.name.lower() for kw in ("studentlist", "student", "roster")) and f.suffix.lower() in (".xlsx", ".xls", ".csv", ".txt"):
            shutil.copy2(f, dst / f.name)
            continue


# ---------------------------------------------------------------------------
# Resume-from-step helpers
# ---------------------------------------------------------------------------

def _copy_artifacts(src: Path, dst: Path, from_step: int, blueprint_step: int) -> None:
    """Copy prior-run artifacts needed for resuming from *from_step* into *dst*.

    Patterns include both the new per-step folder layout and the pre-restructure
    flat layout so that resuming from old runs still works.
    """
    import shutil
    patterns = [
        # New per-step folder paths
        "03_read_student_list/students.*",
        "07_deskew/cleaned_scan.pdf",
        "08_exam_geometry/exam_geometry.*",
        "11_student_names/exam_student_list.*",
        "14_blank_pages/blank_pages.json",
        "15_detect_exam_layout/exam_layout.*",
        "15_detect_exam_layout/split_exam.pdf",
        "17_parse_exam_pdf/exam_questions.*",
        "17_parse_exam_pdf/exam_input.pdf",
        "18_parse_mark_scheme/mark_scheme.*",
        "19_create_report/report.*",
        "19_create_report/short_report.*",
        # Pre-restructure legacy flat paths (backward compatibility)
        "3_students.*",
        "7_cleaned_scan.pdf",
        "8_exam_geometry.*", "8_exam_student_list.*", "8_blank_pages.json",
        "9_exam_layout.*", "9_exam_input.pdf", "9_split_exam.pdf",
        "10_exam_questions.*", "11_mark_scheme.*",
        "12_report.*", "12_short_report.*",
    ]
    if from_step >= blueprint_step + 1:   # from marking — need blueprints
        patterns += [
            f"{blueprint_step:02d}_ai_marking_blueprints/blueprint_page_*.*",
            "13_ai_marking_blueprint_*.*",   # legacy
        ]
    if from_step >= blueprint_step + 2:   # from reports — need marking results
        patterns += [
            "21_ai_marking/students/",
            "students/14_marked_*.*", "students/14_failed_*.*",  # legacy
        ]
    for pat in patterns:
        for src_file in src.glob(pat):
            dst_file = dst / src_file.relative_to(src)
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dst_file)  # copy2 preserves mtime (scaffold cache validity)


def _resume_pipeline(ctx: "_Ctx") -> None:
    """Bootstrap *ctx* from a prior run's artifacts and set ctx.from_step skip logic."""
    # Resolve prior run dir
    resume_dir = ctx.resume_dir
    if resume_dir is None:
        assert ctx.folder is not None
        exam_output_root = Path("output") / "xscore" / ctx.folder.name.replace(" ", "_")
        def _is_valid_run(p: Path) -> bool:
            return (
                (p / "19_create_report" / "report.xml").exists() or   # current
                (p / "18_create_report" / "report.xml").exists() or   # post-step-split legacy
                (p / "17_create_report" / "report.xml").exists() or   # post-step-16 refactor legacy
                (p / "16_create_report" / "report.xml").exists() or   # pre-step-16 refactor legacy
                (p / "12_report.json").exists()                        # pre-restructure legacy
            )
        candidates = sorted(
            (p for p in exam_output_root.iterdir()
             if p.is_dir() and p != ctx.artifact_dir and _is_valid_run(p)),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not candidates:
            raise SystemExit(
                f"No valid prior runs found in {exam_output_root}. Use --resume-dir."
            )
        resume_dir = candidates[0]
    ctx.resume_dir = resume_dir

    # Validate from_step
    blueprint_step = 20
    valid_steps = (blueprint_step, blueprint_step + 1, blueprint_step + 2)
    if ctx.from_step not in valid_steps:
        raise SystemExit(
            f"--from-step {ctx.from_step} not supported for this run "
            f"(use {', '.join(str(s) for s in valid_steps)}: blueprints, marking, reports)."
        )

    # Validate required artifacts exist (check new paths first, then pre-restructure legacy)
    def _first_existing(*paths: Path) -> Path | None:
        return next((p for p in paths if p.exists()), None)

    required: list[Path] = []
    for new_p, old_p in [
        (resume_dir / "07_deskew" / "cleaned_scan.pdf",          resume_dir / "7_cleaned_scan.pdf"),
        (resume_dir / "03_read_student_list" / "students.json",   resume_dir / "3_students.json"),
        (resume_dir / "11_student_names" / "exam_student_list.json", resume_dir / "8_exam_student_list.json"),
        (resume_dir / "19_create_report" / "report.xml",          resume_dir / "12_report.json"),
    ]:
        found = _first_existing(new_p, old_p)
        if found:
            required.append(found)
        else:
            required.append(new_p)   # will be reported as missing

    if ctx.from_step >= blueprint_step + 1:
        bp_new = list(resume_dir.glob("20_ai_marking_blueprints/blueprint_page_*.json"))
        bp_old = list(resume_dir.glob("18_ai_marking_blueprint_*.json"))
        required += bp_new or bp_old
    if ctx.from_step >= blueprint_step + 2:
        mk_new = list(resume_dir.glob("21_ai_marking/students/*.yaml"))
        mk_old = list(resume_dir.glob("students/14_marked_*.xml"))
        required += mk_new or mk_old
    missing = [p for p in required if not p.exists()]
    if missing:
        raise SystemExit(
            f"Prior run {resume_dir} is missing required artifacts:\n"
            + "\n".join(f"  {p.name}" for p in missing)
        )

    # Copy artifacts into new artifact_dir
    assert ctx.artifact_dir is not None
    _copy_artifacts(resume_dir, ctx.artifact_dir, ctx.from_step, blueprint_step)

    # Bootstrap ctx fields from copied artifacts
    from xscore.shared.exam_paths import (
        artifact_exam_student_list_json_path,
        artifact_geometry_json_path,
        artifact_students_json_path,
        STEP_07,
    )
    # Support both new folder path and pre-restructure flat path
    cleaned_new = ctx.artifact_dir / STEP_07 / "cleaned_scan.pdf"
    cleaned_old = ctx.artifact_dir / "7_cleaned_scan.pdf"
    ctx.cleaned_pdf = cleaned_new if cleaned_new.exists() else cleaned_old

    students_path = artifact_students_json_path(ctx.artifact_dir)
    if not students_path.exists():
        students_path = ctx.artifact_dir / "3_students.json"   # pre-restructure fallback
    ctx.students = json.loads(students_path.read_text())

    student_list_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
    if not student_list_path.exists():
        student_list_path = ctx.artifact_dir / "10_student_names" / "exam_student_list.json"  # post-step-16 legacy
    if not student_list_path.exists():
        student_list_path = ctx.artifact_dir / "10_exam_student_list.json"   # transitional
    if not student_list_path.exists():
        student_list_path = ctx.artifact_dir / "8_exam_student_list.json"    # pre-restructure legacy
    page_assignments = json.loads(student_list_path.read_text())
    ctx.page_assignments = page_assignments
    ctx.num_students = len(page_assignments)
    ctx.pages_per_student = max(
        (len(a["page_numbers"]) for a in page_assignments), default=0
    )

    geo_path = artifact_geometry_json_path(ctx.artifact_dir)
    if not geo_path.exists():
        geo_path = ctx.artifact_dir / "8_exam_geometry.json"   # pre-restructure fallback
    if geo_path.exists():
        geo = json.loads(geo_path.read_text())
        ctx.empty_exam_has_cover = geo.get("empty_exam_has_cover")
        ctx.cover_page_mode = bool(geo.get("cover_page_mode", False))

    # Load scaffold from copied report.json (cache hit guaranteed — copy2 preserves mtime)
    from xscore.scaffold.generate_scaffold import build_scaffold
    ctx.scaffold = build_scaffold(
        ctx.folder, artifact_dir=ctx.artifact_dir, force_rebuild=False
    )

    from xscore.shared.terminal_ui import ok_line
    ok_line(f"Resumed from  {resume_dir}  (from-step {ctx.from_step})")


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
        if ctx.from_step is None and inst.from_step is not None:
            ctx.from_step = inst.from_step

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
        if ctx.from_step:
            _resume_pipeline(ctx)
        ok_line(f"Output: {ctx.artifact_dir}")
        (ctx.artifact_dir / "command.txt").write_text(
            "python " + shlex.join([Path(sys.argv[0]).name] + sys.argv[1:]),
            encoding="utf-8",
        )

        # Write step 1 summary now that artifact_dir exists (created here, not in step 1)
        from xscore.shared.exam_paths import artifact_parse_summary_path
        inst = ctx.instruction
        step1_summary = {
            "step": 1,
            "elapsed_s": round(ctx.parse_elapsed, 3),
            "task_type": inst.task_type,
            "dpi": inst.dpi,
            "status": "ok",
        }
        p1 = artifact_parse_summary_path(ctx.artifact_dir)
        p1.parent.mkdir(parents=True, exist_ok=True)
        p1.write_text(json.dumps(step1_summary, indent=2, ensure_ascii=False), encoding="utf-8")

        ok_line(ctx.folder.name)
        validate_input_files(ctx.folder)
        _copy_input_files(ctx.folder, ctx.artifact_dir)

    def _step03_students(ctx: _Ctx, *, on_header_printed=None, on_complete=None) -> None:
        assert ctx.folder is not None and ctx.artifact_dir is not None
        if ctx.from_step:
            return
        pipeline_step(3, "Read student list")
        if on_header_printed is not None:
            on_header_printed()
        ctx.students = read_student_list(ctx.folder, ctx.artifact_dir)
        ok_line(f"{len(ctx.students)} students on the roster")
        write_student_artifacts(ctx.artifact_dir, ctx.students)
        if on_complete is not None:
            on_complete()

    def _scaffold_steps(ctx: _Ctx, *, background: bool = False) -> None:
        """Steps 15–19: detect layout, cut, exam PDF, mark scheme, report merge."""
        assert ctx.folder is not None and ctx.artifact_dir is not None
        if ctx.from_step:
            return
        t0 = time.perf_counter()

        pipeline_step(
            15, "Detect empty exam layout",
            subtitle="running in background" if background else None,
        )

        _step_t = [0.0, 0.0]  # [t_graphics_start, t_scheme_start]

        def _on_layout_done() -> None:
            pipeline_step(
                16, "Cut empty exam",
                subtitle="running in background" if background else None,
            )

        def _on_cut_done(skipped: bool) -> None:
            pipeline_step(
                17, "Parse exam PDF",
                subtitle="running in background" if background else None,
            )

        def _on_exam_done(raw_questions: list) -> None:
            ok_line(f"{len(raw_questions)} top-level questions extracted")
            pipeline_step(
                18, "Parse mark scheme",
                subtitle="completed in background" if background else None,
            )
            _step_t[0] = time.perf_counter()

        def _on_graphics_done(graphics_qs: "list | None") -> None:
            if graphics_qs is None:
                ok_line("Skipped (DETECT_SCHEME_GRAPHICS_MODEL not set)")
            else:
                n = sum(len(q.get("graphics") or []) for q in graphics_qs)
                dur = format_duration(time.perf_counter() - _step_t[0])
                ok_line(f"{n} graphic{'s' if n != 1 else ''} detected  ·  {dur}")
            _step_t[1] = time.perf_counter()

        def _on_scheme_done(scheme_questions: list) -> None:
            dur = format_duration(time.perf_counter() - _step_t[1])
            ok_line(f"{len(scheme_questions)} answers in mark scheme  ·  {dur}")
            pipeline_step(19, "Create report")

        try:
            ctx.scaffold = build_scaffold(
                ctx.folder,
                artifact_dir=ctx.artifact_dir,
                force_rebuild=True,
                on_layout_complete=_on_layout_done,
                on_cut_complete=_on_cut_done,
                on_exam_complete=_on_exam_done,
                on_graphics_complete=_on_graphics_done,
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
        if ctx.from_step:
            return
        ad = ctx.artifact_dir
        dpi = ctx.instruction.dpi

        two = find_two_scan_pdfs(ctx.folder, ad)
        if two is not None:
            pipeline_step(4, "Merge duplex scans")
            match = merge_duplex_scans_phase(two[0], two[1], ad, force_rebuild=ctx.force_clean_scan)
        else:
            match = find_source_scan_match(ctx.folder, ad, dpi)

        from xscore.config import ROTATION_ANALYSIS_DPI
        from xscore.preprocessing.start_scan import _STEP_05, _STEP_06, _STEP_07
        pipeline_step(5, "Detect blank pages")
        t0_7 = time.perf_counter()
        detect_blank_pages_phase(match, ad, analysis_dpi=ROTATION_ANALYSIS_DPI, force_clean_scan=ctx.force_clean_scan)
        _p5 = ad / _STEP_05 / "summary.json"
        _p5.parent.mkdir(parents=True, exist_ok=True)
        _p5.write_text(
            json.dumps({"step": 5, "elapsed_s": round(time.perf_counter() - t0_7, 3), "status": "ok"}, indent=2),
            encoding="utf-8",
        )

        pipeline_step(6, "Autorotate")
        t0_rot = time.perf_counter()
        autorotate_phase(ad)
        elapsed_rot = time.perf_counter() - t0_rot
        _p6 = ad / _STEP_06 / "summary.json"
        _p6.parent.mkdir(parents=True, exist_ok=True)
        _p6.write_text(
            json.dumps({"step": 6, "elapsed_s": round(elapsed_rot, 3), "status": "ok"}, indent=2),
            encoding="utf-8",
        )

        pipeline_step(7, "Deskew")
        t0_9 = time.perf_counter()
        ctx.cleaned_pdf = deskew_phase(ctx.folder, ad, dpi)
        _p7 = ad / _STEP_07 / "summary.json"
        _p7.parent.mkdir(parents=True, exist_ok=True)
        _p7.write_text(
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

    def _kick_off_render_bg(ctx: _Ctx) -> None:
        """Start parallel page rendering in a background thread right after step 10.

        No-op if cleaned_pdf or page_assignments are not yet set.
        """
        if not (ctx.cleaned_pdf and ctx.page_assignments and ctx.artifact_dir):
            return
        from concurrent.futures import ThreadPoolExecutor
        from xscore.config import MARKING_DPI
        from xscore.marking.ai_mark import render_pages_b64
        _instr = getattr(ctx, "instruction", None)
        dpi = getattr(_instr, "dpi", None) or MARKING_DPI
        total_pages = sum(len(a.page_numbers) for a in ctx.page_assignments)
        workers = min(
            total_pages,
            int(os.environ.get("MARKING_WORKERS", str(min(os.cpu_count() or 4, 16)))),
        )
        info_line(f"Pre-rendering {total_pages} page(s) in background ({workers} threads, {dpi} DPI) …")
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="render_bg")
        ctx.b64_future = pool.submit(
            render_pages_b64, ctx.cleaned_pdf, ctx.artifact_dir, dpi, workers,
            instruction=_instr,
        )
        pool.shutdown(wait=False)

    def _run_geometry_steps(ctx: _Ctx) -> None:
        """Steps 3–13 then scaffold (14–16/17) in the main thread."""
        _geo: dict = {}  # geometry from step 8, updated in step 10

        def _step08_scan_geometry() -> None:
            nonlocal _geo
            assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
            if ctx.from_step:
                return
            pipeline_step(8, "Scan geometry")
            exam_pages = ctx.scaffold.page_count if ctx.scaffold else _exam_pdf_page_count(ctx.folder)
            _geo = compute_geometry(ctx.cleaned_pdf, exam_pages, ctx.students or [])
            ctx.num_students = _geo["num_students"]
            ctx.pages_per_student = _geo["pages_per_student"]
            if _geo["roster_mismatch"]:
                info_line(
                    f"Roster has {_geo['num_students_roster']} students "
                    f"but scan implies {_geo['num_students']}"
                )
            ok_line(
                f"{ctx.num_students} students  ·  {ctx.pages_per_student} pages each  "
                f"·  {_geo['scan_pages']} scan pages total"
            )
            # Write immediately so downstream steps can read even if later steps raise.
            _geo["cover_page_mode"] = False
            write_geometry_artifacts(ctx.artifact_dir, _geo)

        def _step09_cover_detection() -> None:
            assert ctx.artifact_dir is not None and ctx.folder is not None
            if ctx.from_step:
                return
            pipeline_step(9, "Cover page")
            try:
                import os as _os
                from xscore.scaffold.generate_scaffold import find_exam_pdf
                from google import genai as gai
                from eXercise.ai_client import parse_model_effort
                from xscore.marking.assign_pages_to_students import check_cover_page_text
                _exam_pdf = find_exam_pdf(ctx.folder)
                _ec_api_key = (_os.environ.get("GEMINI_API_KEY", "") or _os.environ.get("GOOGLE_API_KEY", "")).strip()
                if _ec_api_key:
                    _gai_client_ec = gai.Client(api_key=_ec_api_key)
                    _ec_model, _ec_effort = parse_model_effort(_os.environ.get("EMPTY_EXAM_COVER_MODEL", "gemini-2.5-flash"))
                    from xscore.shared.exam_paths import artifact_cover_page_dir as _cpd
                    _ec_save_dir = _cpd(ctx.artifact_dir)
                    _ec_save_dir.mkdir(parents=True, exist_ok=True)
                    _ec_save = _ec_save_dir / "cover_empty_exam_prompt.md"
                    _t_ec = time.perf_counter()
                    ctx.empty_exam_has_cover = check_cover_page_text(
                        _exam_pdf, 0, _gai_client_ec, _ec_model,
                        prompt_save_path=_ec_save,
                        effort=_ec_effort,
                    )
                    ok_line(
                        f"Empty exam page 1: {'cover page' if ctx.empty_exam_has_cover else 'no cover page'}"
                        f"  ·  {format_duration(time.perf_counter() - _t_ec)}"
                    )
            except Exception as _e:
                warn_line(f"Empty exam cover check skipped: {_e}")

        def _step10_cover_scan() -> None:
            assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
            if ctx.from_step:
                return
            pipeline_step(10, "Cover page detection (scan)")
            from xscore.marking.assign_pages_to_students import detect_scan_cover_pages
            cover_page_mode, cover_ok = detect_scan_cover_pages(
                ctx.cleaned_pdf,
                ctx.pages_per_student,
                artifact_dir=ctx.artifact_dir,
            )
            ctx.cover_page_mode = cover_page_mode
            ctx.cover_ok = cover_ok

        def _step11_student_names() -> None:
            nonlocal _geo
            assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
            if ctx.from_step:
                return
            pipeline_step(11, "Student names")
            t0 = time.perf_counter()
            ctx.page_assignments = assign_pages(
                ctx.cleaned_pdf,
                ctx.students or [],
                pages_per_student=ctx.pages_per_student,
                artifact_dir=ctx.artifact_dir,
                cover_page_mode=ctx.cover_page_mode,
            )
            ctx.cover_page_mode = any(
                a.cover_page_number is not None for a in ctx.page_assignments
            )
            _geo["cover_page_mode"] = ctx.cover_page_mode
            write_geometry_artifacts(ctx.artifact_dir, _geo)
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
            if detected != ctx.num_students:
                warn_line(
                    f"Name detection found {detected} students; geometry expected {ctx.num_students}. "
                    "AI marking will use the scan-detected list."
                )
            ctx.step_timings_marking["assign_pages_s"] = time.perf_counter() - t0
            _kick_off_render_bg(ctx)
            ok_line(
                f"{detected} {'student' if detected == 1 else 'students'} detected from scan"
                f"  ·  {answer_pages} answer pages each"
                + ("  ·  cover page mode" if ctx.cover_page_mode else "")
                + f"  ·  {format_duration(time.perf_counter() - t0)}"
            )

        def _step12_page_count_validation() -> None:
            assert ctx.page_assignments is not None
            if ctx.from_step:
                return
            pipeline_step(12, "Page count validation")
            if not _geo.get("pages_valid", True):
                n_detected   = len(ctx.page_assignments)
                scan_pages   = _geo["scan_pages"]
                _cover = any(a.cover_page_number is not None for a in ctx.page_assignments)
                expected_per = _geo["exam_pages"] + (1 if _cover else 0)
                expected_total = n_detected * expected_per
                diff = scan_pages - expected_total
                msg_lines = [
                    "Scan page count mismatch — cannot mark reliably.",
                    "",
                    f"  Empty exam:  {_geo['exam_pages']} pages per student",
                    f"  Detected:    {n_detected} student(s) in scan",
                    f"  Expected:    {n_detected} × {expected_per} pages = {expected_total} pages total",
                    f"  Scan found:  {scan_pages} pages  ({diff:+d})",
                    "",
                    "  Per-student breakdown:",
                ]
                for a in ctx.page_assignments:
                    actual = len(a.page_numbers)
                    marker = "✓" if actual == expected_per else "✗"
                    deficit = (
                        f"  ← MISSING {expected_per - actual} page(s)" if actual < expected_per else
                        f"  ← EXTRA {actual - expected_per} page(s)"   if actual > expected_per else ""
                    )
                    first, last = a.page_numbers[0], a.page_numbers[-1]
                    msg_lines.append(
                        f"    {a.student_name:<22}"
                        f"scan pages {first:>3}–{last:<3}  "
                        f"{actual}/{expected_per} pages  {marker}{deficit}"
                    )
                msg_lines += [
                    "",
                    "  Note: the short block shown above is always the LAST student in the scan.",
                    "  If pages were actually missing from an earlier booklet, the scanner's",
                    "  page shift means a later student appears short. Check all booklets.",
                    "",
                    "  Re-scan the missing page(s) and re-run.",
                ]
                warn_line("\n".join(msg_lines))
                raise SystemExit(1)
            _cover = any(a.cover_page_number is not None for a in ctx.page_assignments)
            n = len(ctx.page_assignments)
            per_str = f"cover + {_geo['exam_pages']} answer" if _cover else f"{_geo['exam_pages']} pages"
            ok_line(f"Page counts valid  ·  {n} × ({per_str}) = {_geo['scan_pages']} total")

        def _step13_page_order() -> None:
            assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None and ctx.folder is not None
            if ctx.from_step:
                return
            pipeline_step(13, "Page order")
            try:
                from xscore.scaffold.generate_scaffold import find_exam_pdf as _fep
                from xscore.marking.page_order_check import check_page_order as _check_order
                _check_order(
                    _fep(ctx.folder),
                    ctx.cleaned_pdf,
                    ctx.page_assignments,
                    artifact_dir=ctx.artifact_dir,
                )
            except SystemExit:
                raise
            except Exception as _e:
                warn_line(f"Page order check skipped: {_e}")

        def _step14_blank_pages() -> None:
            assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None and ctx.folder is not None
            if ctx.from_step:
                return
            pipeline_step(14, "Blank pages")
            try:
                from xscore.scaffold.generate_scaffold import find_exam_pdf as _fep2
                from xscore.marking.blank_page_detection import check_blank_pages as _check_blank
                _check_blank(
                    _fep2(ctx.folder),
                    ctx.cleaned_pdf,
                    ctx.page_assignments,
                    ctx.artifact_dir,
                    empty_exam_has_cover=bool(ctx.empty_exam_has_cover),
                )
            except SystemExit:
                raise
            except Exception as _e:
                warn_line(f"Blank page detection skipped: {_e}")

        _step03_students(ctx)
        if ctx.stop_after <= 3: raise _EarlyExit()
        _scan_phases(ctx)
        if ctx.stop_after <= 7: raise _EarlyExit()
        if ctx.cleaned_pdf:
            _step08_scan_geometry()
            if ctx.stop_after <= 8: raise _EarlyExit()
            _step09_cover_detection()
            if ctx.stop_after <= 9: raise _EarlyExit()
            _step10_cover_scan()
            if ctx.stop_after <= 10: raise _EarlyExit()
            _step11_student_names()
            if ctx.stop_after <= 11: raise _EarlyExit()
            _step12_page_count_validation()
            if ctx.stop_after <= 12: raise _EarlyExit()
            _step13_page_order()
            if ctx.stop_after <= 13: raise _EarlyExit()
            _step14_blank_pages()
            if ctx.stop_after <= 14: raise _EarlyExit()
        _scaffold_steps(ctx)

    def _step17_blueprints(ctx: _Ctx) -> None:
        """Step 20 — Build per-page AI marking blueprints (no AI calls)."""
        assert ctx.scaffold is not None and ctx.artifact_dir is not None
        pipeline_step(20, "AI marking blueprints")
        t0 = time.perf_counter()
        blueprints = build_blueprints(ctx.scaffold, ctx.artifact_dir)
        ok_line(f"{len(blueprints)} page blueprint(s) written")
        ctx.step_timings_marking["blueprints_s"] = time.perf_counter() - t0

    def _step18_mark(ctx: _Ctx) -> None:
        """Step 21 — AI marking: vision calls to fill blueprints for each student page."""
        assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
        pipeline_step(21, "AI marking")
        t0 = time.perf_counter()
        ctx.marking_api_calls = run_ai_marking(ctx, dpi=ctx.instruction.dpi)
        _n_calls = len(ctx.marking_api_calls)
        _n_failed = len(ctx.marking_failures)
        _n_total = _n_calls + _n_failed
        ok_line(
            f"{_n_calls}/{_n_total} pages marked"
            + (f"  ·  {_n_failed} failed" if _n_failed else "")
        )
        ctx.step_timings_marking["marking_s"] = time.perf_counter() - t0

    def _step19_reports(ctx: _Ctx) -> None:
        """Step 22 — Merge per-page results into student + class reports; compile PDFs."""
        assert ctx.scaffold is not None and ctx.artifact_dir is not None
        pipeline_step(22, "Compile reports")
        t0 = time.perf_counter()
        summaries = compile_reports(ctx)
        _known = [s["percentage"] for s in summaries if s["percentage"] is not None]
        _avg_str = f"{round(sum(_known) / len(_known), 1)}%" if _known else "N/A"
        ok_line(f"{len(summaries)} student report(s)  ·  class avg {_avg_str}")
        ctx.step_timings_marking["reports_s"] = time.perf_counter() - t0

    def _step21_timing(ctx: _Ctx) -> None:
        """Step 23 — Print timing summary and evaluate accuracy against ground truth."""
        assert ctx.artifact_dir is not None
        pipeline_step(23, "Timing summary")
        t0 = time.perf_counter()

        if ctx.folder is not None:
            ground_truth = load_ground_truth(ctx.folder, ctx.scaffold)
            if ground_truth and ctx.scaffold:
                student_results = load_student_results_from_reports(ctx.artifact_dir)
                ctx.accuracy_summary = evaluate_results(student_results, ground_truth, ctx.scaffold)
                from xscore.shared.exam_paths import artifact_accuracy_json_path
                _acc_path = artifact_accuracy_json_path(ctx.artifact_dir)
                _acc_path.parent.mkdir(parents=True, exist_ok=True)
                _acc_path.write_text(
                    json.dumps(ctx.accuracy_summary, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                info_line(
                    f"Accuracy: {ctx.accuracy_summary['overall_correct']}/"
                    f"{ctx.accuracy_summary['overall_total']} "
                    f"({ctx.accuracy_summary['overall_accuracy_pct']:.1f}%)"
                )

        ctx.step_timings_marking["timing_s"] = round(time.perf_counter() - t0, 3)
        from xscore.marking.timing_report import print_step_durations
        print_step_durations(ctx.step_timings_marking, ctx.marking_api_calls)

    def _step22_ai_costs(ctx: _Ctx) -> None:
        """Step 24 — Compute AI token costs and write complete timing report artifacts."""
        assert ctx.artifact_dir is not None
        pipeline_step(24, "AI Costs")
        from eXercise.ai_client import get_run_usage
        from xscore.shared.cost_report import compute_cost
        _run_usage = get_run_usage()
        _total_cost, _ = compute_cost(_run_usage)
        write_timing_report(
            ctx.artifact_dir,
            ctx.step_timings_marking,
            ctx.marking_api_calls,
            accuracy_summary=ctx.accuracy_summary,
            failures=ctx.marking_failures,
            token_usage=_run_usage,
            total_cost_rmb=_total_cost,
            print_timing=False,
        )

    # -----------------------------------------------------------------------
    # Run the pipeline
    # -----------------------------------------------------------------------

    from eXercise.ai_client import reset_run_usage
    reset_run_usage()

    ctx = _Ctx(args=args, timestamp=timestamp)
    t0 = time.perf_counter()
    try:
        _step01_parse(ctx)
        if ctx.stop_after <= 1: raise _EarlyExit()
        _step02_folder(ctx)
        if ctx.stop_after <= 2: raise _EarlyExit()
        _run_geometry_steps(ctx)
        if ctx.stop_after <= 19: raise _EarlyExit()
        if ctx.cleaned_pdf and ctx.scaffold:
            _step17_blueprints(ctx)
            if ctx.stop_after <= 20: raise _EarlyExit()
            _step18_mark(ctx)
            if ctx.stop_after <= 21: raise _EarlyExit()
            _step19_reports(ctx)
            if ctx.stop_after <= 22: raise _EarlyExit()
            _step21_timing(ctx)
            if ctx.stop_after <= 23: raise _EarlyExit()
            _step22_ai_costs(ctx)
        elif ctx.cleaned_pdf and not ctx.scaffold:
            warn_line("Marking skipped — scaffold not available (steps 20–24 omitted).")
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
