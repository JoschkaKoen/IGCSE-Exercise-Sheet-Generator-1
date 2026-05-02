#!/usr/bin/env python3
"""
xScore.py
---------
Exam scan grading pipeline — run from the eXercise project root.

The canonical step list lives in ``xscore/shared/pipeline_steps.py`` (the
``STEPS`` registry). Step bodies live in ``xscore/steps/``; orchestration in
``xscore/pipeline/runner.py``. This file is the entry point: argparse, _Tee
log mirror, banner, dispatch.

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
        description="Grade an exam scan.",
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
            "When set, step 30 emits only the filtered students' reports — the class "
            "report is skipped."
        ),
    )
    parser.add_argument(
        "--limit-students",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Mark only the first N students. Composes with --student and any "
            "prompt-derived first_n filter (slice is applied last). When set, "
            "step 30 (class report) is skipped — same reasoning as --student."
        ),
    )
    args = parser.parse_args()
    if args.limit_students is not None and args.limit_students <= 0:
        parser.error(f"--limit-students must be a positive integer (got {args.limit_students})")
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
