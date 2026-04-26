#!/usr/bin/env python3
"""
xScore.py
---------
Exam scan grading pipeline (steps 1–32) — run from the eXercise project root.

Steps:
  1. Parse the natural language prompt (via Kimi).
  2. Locate the exam folder.
  3. Read the student roster from StudentList.xlsx.
  4. Merge duplex scan pairs (scan1+scan2, scan3+scan4, ...) into one PDF when numbered scans are present.
  5. Detect blank scan pages.
  6. Autorotate (remove blanks, apply /Rotate metadata).
  7. Deskew (small-angle per-half correction) → 07_deskew/cleaned_scan.pdf.
  8. AI: cover page detection (empty exam) → 08_cover_page_empty/.
  9. AI: cover page detection (scan, page 1 only) → 09_cover_page_scan/.
 10. Scan geometry (page/student counts) → 10_exam_geometry/exam_geometry.json.
 11. AI: verify cover pages on remaining students → 11_cover_page_verify/.
 12. Student name detection (name OCR) → 12_student_names/exam_student_list.json.
 13. Page order check → 13_page_order/.
 14. Exam blank detection → 14_exam_blank_detection/.
 15. Student handwriting check → 15_student_handwriting/.
 16. AI: detect exam layout → 16_detect_exam_layout/.
 17. Cut exam PDF (split multi-up pages) → 17_cut_exam/split_exam.pdf (skipped for 1×1).
 18. AI: parse exam PDF → question hierarchy → 18_parse_exam_pdf/exam_questions.json.
 19. AI: detect mark scheme graphics → 19_detect_mark_scheme_graphics/mark_scheme_graphics.json.
 20. AI: assign questions to mark scheme pages → 20_assign_scheme_questions/questions_per_page.json.
 21. AI: parse mark scheme → correct answers + criteria → 21_parse_mark_scheme/mark_scheme.json.
 22. Merge scaffold → 22_create_report/report.json.
 23. Build per-page AI marking blueprints → 23_ai_marking_blueprints/.
 24. AI: grade each student page → 24_ai_marking/students/.
 25. Per-student reports (XML + MD) → 25_student_report_preparation/<student>/.
 26. Class statistics + grade curve → 26_class_stats/class_stats.json.
 27. Per-student PDFs (xelatex) → 27_student_pdfs/<student>/.
 28. Class report (XML/MD/TeX/PDF + combined PDF) → 28_class_report/.
 29. Review queue (medium/low confidence marks) → 29_review_queue/.
 30. Timing summary → 30_timing_summary/timing.json.
 31. Accuracy evaluation (no-op when no ground truth) → 31_accuracy/accuracy.json.
 32. AI Costs → 32_ai_costs/cost.json + cost.md.

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
        description="Grade an exam scan (steps 1–32).",
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
            "When set, step 24 emits only the filtered students' reports — the class "
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
