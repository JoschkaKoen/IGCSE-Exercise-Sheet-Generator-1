"""Steps 8–14: scan geometry, cover-page detection, student name OCR, validations.

Step 11 keeps a named ``assign_pages_s`` sub-timing for the timing report
(in addition to the canonical ``student_names`` timing run_step writes).

Steps 13 and 14 catch their own exceptions and warn-and-continue — the
``try/except SystemExit/Exception`` pattern stays inside the body so
``run_step`` only sees an exception escape on truly unrecoverable failures.

Step 12 raises ``SystemExit(1)`` on page-count mismatch — ``run_step``
re-raises after capture, so the process still terminates as today.
"""

from __future__ import annotations

import logging
import os
import time

from xscore.preprocessing.assign_pages_to_students import (
    assign_pages,
    check_cover_page_text,
    detect_scan_cover_pages,
    page_assignments_to_json,
    page_assignments_to_md,
)
from xscore.marking.blank_page_detection import check_blank_pages
from xscore.marking.geometry import compute_geometry, write_geometry_artifacts
from xscore.marking.page_order_check import check_page_order
from xscore.pipeline.resume import exam_pdf_page_count
from xscore.scaffold.generate_scaffold import find_exam_pdf
from xscore.shared.exam_paths import (
    artifact_cover_page_dir,
    artifact_exam_student_list_json_path,
    artifact_exam_student_list_md_path,
)
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.terminal_ui import format_duration, info_line, ok_line, warn_line


def step_08_geometry(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    exam_pages = ctx.scaffold.page_count if ctx.scaffold else exam_pdf_page_count(ctx.folder)
    ctx.geo = compute_geometry(ctx.cleaned_pdf, exam_pages, ctx.students or [])
    ctx.num_students = ctx.geo["num_students"]
    ctx.pages_per_student = ctx.geo["pages_per_student"]
    if ctx.geo["roster_mismatch"]:
        info_line(
            f"Roster has {ctx.geo['num_students_roster']} students "
            f"but scan implies {ctx.geo['num_students']}"
        )
    stu_word = "student" if ctx.num_students == 1 else "students"
    ok_line(
        f"{ctx.num_students} {stu_word}  ·  {ctx.pages_per_student} pages each  "
        f"·  {ctx.geo['scan_pages']} scan pages total"
    )
    # Write immediately so downstream steps can read even if later steps raise.
    # cover_page_mode is intentionally NOT written here — step 11 finalizes
    # it after detection and persists then.
    write_geometry_artifacts(ctx.artifact_dir, ctx.geo)


def step_09_cover_empty(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None and ctx.folder is not None
    try:
        from google import genai as gai
        from eXercise.ai_client import parse_model_effort
    except ImportError as exc:
        warn_line(f"Empty exam cover check skipped — google-genai not installed: {exc}")
        return
    exam_pdf = find_exam_pdf(ctx.folder)
    api_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
    if not api_key:
        warn_line("Empty exam cover check skipped — no GEMINI_API_KEY")
        return
    try:
        gai_client = gai.Client(api_key=api_key)
        from xscore.config import EMPTY_EXAM_COVER_MODEL
        model, effort = parse_model_effort(EMPTY_EXAM_COVER_MODEL)
        save_dir = artifact_cover_page_dir(ctx.artifact_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        save = save_dir / "cover_empty_exam_prompt.md"
        t0 = time.perf_counter()
        ctx.empty_exam_has_cover = check_cover_page_text(
            exam_pdf, 0, gai_client, model,
            prompt_save_path=save,
            effort=effort,
        )
        ok_line(
            f"Empty exam page 1: {'cover page' if ctx.empty_exam_has_cover else 'no cover page'}"
            f"  ·  {format_duration(time.perf_counter() - t0)}"
        )
    except Exception:
        logging.exception("step 9 cover detection failed")
        raise


def step_10_cover_scan(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    cover_page_mode, _cover_ok = detect_scan_cover_pages(
        ctx.cleaned_pdf,
        ctx.pages_per_student,
        artifact_dir=ctx.artifact_dir,
    )
    ctx.cover_page_mode = cover_page_mode


def step_11_student_names(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
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
    ctx.geo["cover_page_mode"] = ctx.cover_page_mode
    write_geometry_artifacts(ctx.artifact_dir, ctx.geo)
    json_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
    json_path.write_text(page_assignments_to_json(ctx.page_assignments), encoding="utf-8")
    md_path = artifact_exam_student_list_md_path(ctx.artifact_dir)
    md_path.write_text(page_assignments_to_md(ctx.page_assignments), encoding="utf-8")
    detected = len(ctx.page_assignments)
    answer_pages = ctx.pages_per_student - (1 if ctx.cover_page_mode else 0)
    if detected != ctx.num_students:
        warn_line(
            f"Name detection found {detected} students; geometry expected {ctx.num_students}. "
            "AI marking will use the scan-detected list."
        )
    ok_line(
        f"{detected} {'student' if detected == 1 else 'students'} detected from scan"
        f"  ·  {answer_pages} answer pages each"
        + ("  ·  cover page mode" if ctx.cover_page_mode else "")
        + f"  ·  {format_duration(time.perf_counter() - t0)}"
    )


def step_12_page_count(ctx: _Ctx) -> None:
    assert ctx.page_assignments is not None
    if not ctx.geo.get("pages_valid", True):
        n_detected = len(ctx.page_assignments)
        scan_pages = ctx.geo["scan_pages"]
        cover = any(a.cover_page_number is not None for a in ctx.page_assignments)
        expected_per = ctx.geo["exam_pages"] + (1 if cover else 0)
        expected_total = n_detected * expected_per
        diff = scan_pages - expected_total
        msg_lines = [
            "Scan page count mismatch — cannot mark reliably.",
            "",
            f"  Empty exam:  {ctx.geo['exam_pages']} pages per student",
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
    cover = any(a.cover_page_number is not None for a in ctx.page_assignments)
    n = len(ctx.page_assignments)
    pps = ctx.geo["pages_per_student"]
    per_str = f"cover + {pps - 1} answer" if cover else f"{pps} pages"
    ok_line(f"Page counts valid  ·  {n} × ({per_str}) = {ctx.geo['scan_pages']} total")


def step_13_page_order(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None and ctx.folder is not None
    try:
        check_page_order(
            find_exam_pdf(ctx.folder),
            ctx.cleaned_pdf,
            ctx.page_assignments,
            artifact_dir=ctx.artifact_dir,
        )
    except SystemExit:
        raise
    except Exception as exc:
        warn_line(f"Page order check skipped: {exc}")


def step_14_blank_pages(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None and ctx.folder is not None
    try:
        check_blank_pages(
            find_exam_pdf(ctx.folder),
            ctx.cleaned_pdf,
            ctx.page_assignments,
            ctx.artifact_dir,
            empty_exam_has_cover=bool(ctx.empty_exam_has_cover),
        )
    except SystemExit:
        raise
    except Exception as exc:
        warn_line(f"Blank page detection skipped: {exc}")
