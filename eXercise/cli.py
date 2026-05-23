# -*- coding: utf-8 -*-
"""Command-line entry point."""

import argparse
import shlex
import sys
import time

from .env_load import load_project_env
from .exceptions import ExtractionUserError
from .natural_language import resolve_natural_language
from .output_paths import resolve_output_path, set_run_command
from .pipeline import run_extraction, run_extraction_jobs


def _parse_question_tokens(tokens: list[str]) -> list[int]:
    """Parse question tokens like ['1', '3-5', '7'] into [1, 3, 4, 5, 7]."""
    requested = []
    for arg in tokens:
        # Normalise unicode dashes so copy-pasted ranges (e.g. "12–14") work.
        arg = arg.replace("\u2013", "-").replace("\u2014", "-")
        if "-" in arg and not arg.startswith("-"):
            parts = arg.split("-")
            if len(parts) != 2:
                print(f"Invalid range: {arg}", file=sys.stderr)
                sys.exit(1)
            try:
                start, end = int(parts[0]), int(parts[1])
            except ValueError:
                print(f"Invalid range '{arg}': both endpoints must be integers", file=sys.stderr)
                sys.exit(1)
            if start > end:
                print(f"Invalid range '{arg}': start must be ≤ end", file=sys.stderr)
                sys.exit(1)
            requested.extend(range(start, end + 1))
        else:
            try:
                requested.append(int(arg))
            except ValueError:
                print(f"Invalid question number: {arg}", file=sys.stderr)
                sys.exit(1)
    return requested


def _print_timing_summary(steps: list[tuple[str, float]], total: float) -> None:
    """Print a per-step timing table followed by the total."""
    if not steps:
        print(f"\n  Total  {total:.1f}s")
        return
    name_w = max(len(s[0]) for s in steps)
    name_w = max(name_w, 5)  # at least width of "Total"
    col = name_w + 8          # padding between name and time
    sep = "  " + "─" * (col + 4)
    print()
    for name, elapsed in steps:
        print(f"  {name:<{name_w}}  {elapsed:>5.1f}s")
    print(sep)
    print(f"  {'Total':<{name_w}}  {total:>5.1f}s")


def _print_cost_tables(rec) -> None:
    """Print per-phase + per-model cost tables. No-op when the recorder is null
    or no AI calls fired (e.g. a legacy-mode run with no MCQ papers or ranking)."""
    if rec.is_null or not rec.total_usage:
        return
    from .cost_report import build_per_phase_breakdown, compute_cost
    from .cost_table import print_cost_table, print_per_step_cost_table

    total_cost, breakdown = compute_cost(rec.total_usage)
    per_phase = build_per_phase_breakdown(rec.per_phase_usage, rec.per_phase_calls)
    print_per_step_cost_table(per_phase)
    print_cost_table(
        breakdown,
        sum(v["input"]    for v in rec.total_usage.values()),
        sum(v["output"]   for v in rec.total_usage.values()),
        sum(v.get("thinking", 0) for v in rec.total_usage.values()),
        total_cost,
    )


def main():
    load_project_env()
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
    parser.add_argument(
        "--ranking",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Generate a difficulty-ranking PDF. Default: off. "
            "Can also be enabled by phrasing it in the natural-language prompt "
            "(e.g. 'with ranking'). Use --no-ranking to force off, overriding the prompt."
        ),
    )

    args = parser.parse_args()
    parts = args.parts
    set_run_command(shlex.join(sys.argv))

    from .cost_recorder import collect_run_cost

    _step_timings: list[tuple[str, float]] = []
    _t0 = time.monotonic()
    with collect_run_cost() as rec:
        try:
            if len(parts) == 1:
                if args.mark_scheme:
                    parser.error("--ms applies only to legacy mode (three or more arguments).")
                instruction = parts[0]
                _t = time.monotonic()
                with rec.phase("Resolve instruction"):
                    exam_root, data = resolve_natural_language(instruction)
                _step_timings.append(("Resolve instruction", time.monotonic() - _t))
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
                run_ranking = (
                    args.ranking if args.ranking is not None
                    else data.get("ranking", False)
                )
                run_extraction_jobs(
                    jobs, output_pdf,
                    exam_key=data.get("exam"),
                    run_ranking=run_ranking,
                    step_timings=_step_timings,
                )
                return

            if len(parts) < 3:
                parser.error(
                    "Pass one quoted sentence describing what to extract, "
                    "or at least: input_pdf output_pdf QUESTION [QUESTION ...]"
                )

            input_pdf = parts[0]
            output_pdf = str(resolve_output_path(parts[1]))
            requested = _parse_question_tokens(parts[2:])
            run_extraction(
                input_pdf, output_pdf, requested, args.mark_scheme,
                run_ranking=bool(args.ranking),
                step_timings=_step_timings,
            )
        except ExtractionUserError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
        finally:
            _print_timing_summary(_step_timings, time.monotonic() - _t0)
            _print_cost_tables(rec)
