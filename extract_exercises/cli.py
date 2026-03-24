# -*- coding: utf-8 -*-
"""Command-line entry point."""

import argparse
import sys

from .exceptions import ExtractionUserError
from .natural_language import resolve_natural_language
from .output_paths import resolve_output_path
from .pipeline import run_extraction, run_extraction_jobs


def _parse_question_tokens(tokens: list[str]) -> list[int]:
    """Parse question tokens like ['1', '3-5', '7'] into [1, 3, 4, 5, 7]."""
    requested = []
    for arg in tokens:
        if "-" in arg and not arg.startswith("-"):
            parts = arg.split("-")
            if len(parts) != 2:
                print(f"Invalid range: {arg}", file=sys.stderr)
                sys.exit(1)
            start, end = int(parts[0]), int(parts[1])
            requested.extend(range(start, end + 1))
        else:
            requested.append(int(arg))
    return requested


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Pass one plain-English sentence: the script chooses subject, PDFs, and questions. "
            "Alternatively pass explicit paths (legacy)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See the package docstring or README for venv activation and examples.",
    )
    parser.add_argument(
        "parts",
        nargs="+",
        metavar="ARG",
        help="Natural language (quote as one argument), or legacy: input_pdf output_pdf QUESTION ...",
    )
    parser.add_argument(
        "--ms",
        dest="mark_scheme",
        metavar="PDF",
        help="Legacy only: mark scheme PDF after explicit input/output/questions.",
    )

    args = parser.parse_args()
    parts = args.parts

    try:
        if len(parts) == 1:
            if args.mark_scheme:
                parser.error("--ms applies only to legacy mode (three or more arguments).")
            instruction = parts[0]
            exam_root, data = resolve_natural_language(instruction)
            print(f"Exam folder: {exam_root} ({data.get('exam', '')})")
            print(f"Papers in this run: {len(data['extractions'])}")

            jobs = []
            for ex in data["extractions"]:
                jobs.append(
                    {
                        "input_pdf": str(exam_root / ex["input_pdf"]),
                        "questions": ex["questions"],
                        "mark_scheme_pdf": str(exam_root / ex["mark_scheme_pdf"])
                        if ex.get("mark_scheme_pdf")
                        else None,
                    }
                )
            output_pdf = str(resolve_output_path(data["output_pdf"]))
            run_extraction_jobs(jobs, output_pdf, exam_key=data.get("exam"))
            return

        if len(parts) < 3:
            parser.error(
                "Pass one quoted sentence describing what to extract, "
                "or at least: input_pdf output_pdf QUESTION [QUESTION ...]"
            )

        input_pdf = parts[0]
        output_pdf = str(resolve_output_path(parts[1]))
        requested = _parse_question_tokens(parts[2:])
        run_extraction(input_pdf, output_pdf, requested, args.mark_scheme)
    except ExtractionUserError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
