#!/usr/bin/env python3
"""
xScore.py
---------
Exam scan grading pipeline (steps 1–31) — run from the eXercise project root.

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
 14. Exam blank detection → 14_exam_blank_detection/.
 15. Student handwriting check → 15_student_handwriting/.
 16. AI: detect exam layout → 16_detect_exam_layout/.
 17. Cut exam PDF (split multi-up pages) → 17_cut_exam/split_exam.pdf (skipped for 1×1).
 18. AI: parse exam PDF → question hierarchy → 18_parse_exam_pdf/exam_questions.json.
 19. AI: detect mark scheme graphics → 19_detect_mark_scheme_graphics/mark_scheme_graphics.json.
 20. AI: parse mark scheme → correct answers + criteria → 20_parse_mark_scheme/mark_scheme.json.
 21. Merge scaffold → 21_create_report/report.json.
 22. Build per-page AI marking blueprints → 22_ai_marking_blueprints/.
 23. AI: grade each student page → 23_ai_marking/students/.
 24. Per-student reports (XML + MD) → 24_student_reports/students/.
 25. Class statistics + grade curve → 25_class_stats/class_stats.json.
 26. Per-student PDFs (xelatex) → 26_student_pdfs/students/.
 27. Class report (XML/MD/TeX/PDF + combined PDF) → 27_class_report/.
 28. Review queue (medium/low confidence marks) → 28_review_queue/.
 29. Timing summary → 29_timing_summary/timing.json.
 30. Accuracy evaluation (no-op when no ground truth) → 30_accuracy/accuracy.json.
 31. AI Costs → 31_ai_costs/cost.json + cost.md.

Step bodies live in xscore/steps/, orchestration in xscore/pipeline/runner.py.
This file is the entry point: argparse, _Tee log mirror, banner, dispatch.

Usage:
    python xScore.py "grade Space Physics Unit Test"
    python xScore.py "grade the exam" --folder "exams/space_physics" --dpi 300
"""

from __future__ import annotations

import argparse
import datetime
import re
import shlex
import sys
from pathlib import Path

from dotenv import load_dotenv

__version__ = "0.5"


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
        description="Grade an exam scan (steps 1–31).",
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
    parser.add_argument(
        "--student",
        action="append",
        default=None,
        metavar="NAME",
        help=(
            "Mark only the given student (case-insensitive exact match). "
            "Repeat the flag or pass a comma-separated list to mark a small cohort. "
            "When set, step 23 emits only the filtered students' reports — the class "
            "report is skipped."
        ),
    )
    args = parser.parse_args()
    return args


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
        from xscore.pipeline.runner import run_pipeline
        run_pipeline(args, timestamp, log_path=log_path)
    finally:
        tee.flush()
        tee.close()


if __name__ == "__main__":
    main()
