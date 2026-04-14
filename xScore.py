#!/usr/bin/env python3
"""
xScore.py
---------
Exam scan preparation pipeline (steps 1–7) — run from the eXercise project root.

Steps:
  1. Parse the natural language prompt (via Kimi).
  2. Locate the exam folder.
  3. Read the student roster from StudentList.xlsx.
  4. Build exam scaffold (optional; requires vector exam PDF).
  5. Detect blank scan pages.
  6. Autorotate (remove blanks, apply /Rotate metadata).
  7. Deskew (small-angle per-half correction) → 3_cleaned_scan.pdf.

Usage:
    python xScore.py "grade Space Physics Unit Test"
    python xScore.py "grade the exam" --folder "exams/space_physics" --dpi 300
    python xScore.py "grade" --folder "exams/space_physics" --through-step 3
"""

from __future__ import annotations

import argparse
import datetime
import re
import shlex
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from dotenv import load_dotenv

__version__ = "0.1"

_VALID_THROUGH_STEPS = [1, 2, 3, 4, 5, 6, 7]


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
        description="Prepare an exam scan (steps 1–3 and 5–7).",
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
        "--skip-clean-scan",
        action="store_true",
        default=False,
        help="Reuse existing 3_cleaned_scan.pdf (skip steps 5–7)",
    )
    parser.add_argument(
        "--force-clean-scan",
        action="store_true",
        default=False,
        help="Rebuild cleaned scan even if cached",
    )
    parser.add_argument(
        "--through-step",
        type=int,
        default=None,
        metavar="N",
        choices=_VALID_THROUGH_STEPS,
        help=f"Exit after step N (choices: {_VALID_THROUGH_STEPS})",
    )
    args = parser.parse_args()
    if args.skip_clean_scan and args.force_clean_scan:
        parser.error("--skip-clean-scan and --force-clean-scan cannot be used together.")
    return args


# ---------------------------------------------------------------------------
# Context dataclass
# ---------------------------------------------------------------------------

@dataclass
class _Ctx:
    args: argparse.Namespace
    timestamp: str
    client: Any = None
    instruction: Any = None          # TaskInstruction
    parse_elapsed: float = 0.0
    skip_clean_scan: bool = False
    force_clean_scan: bool = False
    through_step: int | None = None
    folder: Path | None = None
    artifact_dir: Path | None = None
    students: list[str] | None = None
    scaffold: Any = None
    cleaned_pdf: Path | None = None
    partial_stop_step: int | None = None
    pipeline_completed_ok: bool = False


def _print_footer(ctx: _Ctx, gi: SimpleNamespace, elapsed: float) -> None:
    t = f"{elapsed:.1f}s"
    if ctx.partial_stop_step is not None:
        gi.info_line(f"Run · {t} · partial step {ctx.partial_stop_step}")
    elif ctx.pipeline_completed_ok:
        gi.info_line(f"Run · {t} · complete")
    else:
        gi.info_line(f"Run · {t}")
    gi.get_console().print()
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Lazy imports (after load_dotenv so env vars are available)
# ---------------------------------------------------------------------------

def _load_imports() -> SimpleNamespace:
    from xscore.extraction.providers.kimi import KimiProvider
    from xscore.marking.find_exam_folder import find_folder
    from xscore.marking.parse_instruction import parse_prompt
    from xscore.preprocessing.start_scan import (
        CLEANED_SCAN_PDF,
        autorotate_phase,
        deskew_phase,
        detect_blank_pages_phase,
        find_source_scan_match,
    )
    from xscore.scaffold.generate_scaffold import build_scaffold
    from xscore.shared.load_student_list import read_student_list
    from xscore.shared.terminal_ui import (
        err_line,
        format_duration,
        get_console,
        info_line,
        ok_line,
        pipeline_step,
        warn_line,
    )

    return SimpleNamespace(
        KimiProvider=KimiProvider,
        find_folder=find_folder,
        parse_prompt=parse_prompt,
        build_scaffold=build_scaffold,
        CLEANED_SCAN_PDF=CLEANED_SCAN_PDF,
        autorotate_phase=autorotate_phase,
        deskew_phase=deskew_phase,
        detect_blank_pages_phase=detect_blank_pages_phase,
        find_source_scan_match=find_source_scan_match,
        read_student_list=read_student_list,
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

def _create_client(ctx: _Ctx, gi: SimpleNamespace) -> None:
    ctx.client = gi.KimiProvider.create_client()
    if ctx.client is None:
        gi.err_line("Could not create Kimi API client.")
        gi.err_line("Set KIMI_API_KEY in your .env file or environment.")
        raise SystemExit(1)


def _step01_parse(ctx: _Ctx, gi: SimpleNamespace) -> None:
    from xscore.config import pipeline_ai_model_display_name
    gi.pipeline_step(1, "Analyzing your request")
    gi.info_line(f"Parsing prompt with {pipeline_ai_model_display_name()} …")
    t0 = time.perf_counter()
    ctx.instruction = gi.parse_prompt(ctx.args.prompt, client=ctx.client, dpi_override=ctx.args.dpi)
    ctx.parse_elapsed = time.perf_counter() - t0
    assert ctx.instruction is not None
    inst = ctx.instruction

    ctx.skip_clean_scan = ctx.args.skip_clean_scan or inst.skip_clean_scan
    ctx.force_clean_scan = ctx.args.force_clean_scan or inst.force_clean_scan
    if ctx.skip_clean_scan and ctx.force_clean_scan:
        gi.err_line("Cannot combine skip and force class-scan cleaning.")
        raise SystemExit(1)
    ctx.through_step = (
        ctx.args.through_step if ctx.args.through_step is not None else inst.through_step
    )
    # Clamp through_step to valid range for this script
    if ctx.through_step is not None and ctx.through_step not in _VALID_THROUGH_STEPS:
        gi.warn_line(f"through_step={ctx.through_step} not in {_VALID_THROUGH_STEPS}; ignoring.")
        ctx.through_step = None

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

    if ctx.through_step == 1:
        ctx.partial_stop_step = 1
        raise SystemExit(0)


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
    exam_output_root = Path("output") / stem
    exam_output_root.mkdir(parents=True, exist_ok=True)
    ctx.artifact_dir = exam_output_root / ctx.timestamp
    suffix = 1
    while ctx.artifact_dir.exists():
        suffix += 1
        ctx.artifact_dir = exam_output_root / f"{ctx.timestamp}_{suffix}"
    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    gi.ok_line(ctx.folder.name)
    if ctx.through_step == 2:
        ctx.partial_stop_step = 2
        raise SystemExit(0)


def _step03_students(ctx: _Ctx, gi: SimpleNamespace) -> None:
    assert ctx.folder is not None
    gi.pipeline_step(3, "Read student list")
    ctx.students = gi.read_student_list(ctx.folder)
    gi.ok_line(f"{len(ctx.students)} students on the roster")
    if ctx.through_step == 3:
        ctx.partial_stop_step = 3
        raise SystemExit(0)


def _step04_scaffold(ctx: _Ctx, gi: SimpleNamespace) -> None:
    assert ctx.folder is not None and ctx.artifact_dir is not None
    gi.pipeline_step(4, "Build exam scaffold")
    try:
        t0 = time.perf_counter()
        ctx.scaffold = gi.build_scaffold(ctx.folder, artifact_dir=ctx.artifact_dir)
        elapsed = time.perf_counter() - t0
        qs = ctx.scaffold.gradable_questions
        gi.ok_line(
            f"{len(qs)} gradable parts  ·  {ctx.scaffold.total_marks} marks total"
            f"  ·  {gi.format_duration(elapsed)}"
        )
    except FileNotFoundError as exc:
        gi.warn_line(f"No exam PDF found — scaffold skipped ({exc})")
    if ctx.through_step == 4:
        ctx.partial_stop_step = 4
        raise SystemExit(0)


def _scan_phases(ctx: _Ctx, gi: SimpleNamespace) -> None:
    """Steps 5–7: blank detection → autorotate → deskew."""
    assert ctx.folder is not None and ctx.artifact_dir is not None and ctx.instruction is not None
    ad = ctx.artifact_dir
    dpi = ctx.instruction.dpi
    cleaned_path = ad / gi.CLEANED_SCAN_PDF

    if ctx.skip_clean_scan:
        gi.pipeline_step(5, "Detect blank pages")
        legacy_cleaned = ctx.folder / gi.CLEANED_SCAN_PDF
        if cleaned_path.exists():
            ctx.cleaned_pdf = cleaned_path
            gi.info_line("Using existing cleaned scan (skip) — scan steps skipped.")
        elif legacy_cleaned.exists():
            ctx.cleaned_pdf = legacy_cleaned
            gi.info_line("Using existing cleaned scan (skip) — scan steps skipped.")
        else:
            scans = [f for f in ctx.folder.glob("*.pdf") if "scan" in f.name.lower()]
            if not scans:
                gi.err_line("--skip-clean-scan set but no scan PDF found.")
                raise SystemExit(1)
            ctx.cleaned_pdf = scans[0]
            gi.info_line("Using existing scan PDF (skip) — scan steps skipped.")
        return

    match = gi.find_source_scan_match(ctx.folder, ad, dpi)

    # Use cache when doing a full run (not stopping mid-scan)
    partial_scan = ctx.through_step is not None and 5 <= ctx.through_step <= 7
    cache_ok = (
        not partial_scan
        and not ctx.force_clean_scan
        and cleaned_path.is_file()
        and cleaned_path.stat().st_mtime >= match.stat().st_mtime
    )
    if cache_ok:
        gi.pipeline_step(5, "Detect blank pages")
        gi.info_line("Using cached cleaned scan (steps 5–7 skipped).")
        ctx.cleaned_pdf = cleaned_path
        return

    gi.pipeline_step(5, "Detect blank pages")
    gi.detect_blank_pages_phase(match, ad, analysis_dpi=dpi, force_clean_scan=ctx.force_clean_scan)
    if ctx.through_step == 5:
        ctx.partial_stop_step = 5
        raise SystemExit(0)

    gi.pipeline_step(6, "Autorotate")
    t0_rot = time.perf_counter()
    gi.autorotate_phase(ad)
    gi.info_line(gi.format_duration(time.perf_counter() - t0_rot))
    if ctx.through_step == 6:
        ctx.partial_stop_step = 6
        raise SystemExit(0)

    gi.pipeline_step(7, "Small angle correction")
    ctx.cleaned_pdf = gi.deskew_phase(ctx.folder, ad, dpi)
    if ctx.through_step == 7:
        ctx.partial_stop_step = 7
        raise SystemExit(0)


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------

def _run(args: argparse.Namespace, timestamp: str) -> None:
    gi = _load_imports()
    ctx = _Ctx(args=args, timestamp=timestamp)
    t0 = time.perf_counter()
    try:
        _create_client(ctx, gi)
        _step01_parse(ctx, gi)
        _step02_folder(ctx, gi)
        _step03_students(ctx, gi)
        _step04_scaffold(ctx, gi)
        _scan_phases(ctx, gi)
        gi.ok_line("Pipeline complete.")
        ctx.pipeline_completed_ok = True
        if ctx.cleaned_pdf:
            gi.info_line(f"Cleaned scan: {ctx.cleaned_pdf}")
    finally:
        _print_footer(ctx, gi, time.perf_counter() - t0)


def main() -> None:
    load_dotenv()
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
