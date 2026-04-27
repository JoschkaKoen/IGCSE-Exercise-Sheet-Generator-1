"""Steps 8–15: cover-page detection, scan geometry, student name OCR, validations.

Cover detection now runs in two phases. Step 9 checks scan page 1 only and
sets ``ctx.cover_page_mode``; step 10 then derives ``pages_per_student``
deterministically from that flag and aborts on any total-page mismatch.
Step 11 verifies the remaining cover positions in parallel (warn-by-default,
fail-fast under ``COVER_PAGE_VERIFY_STRICT=1``).

Steps 13, 14, and 15 return ``(status, message)`` from their helpers; the
dispatchers below own the policy. INCONCLUSIVE → loud warn + continue
(or ``SystemExit(1)`` when the per-step ``*_STRICT=1`` env var is set).
MISMATCH_FOUND in step 13 still raises ``SystemExit(1)``.
"""

from __future__ import annotations

import json
import logging
import os
import time

from xscore.preprocessing.assign_pages_to_students import (
    assign_pages,
    detect_empty_exam_cover,
    detect_first_page_cover,
    page_assignments_to_json,
    page_assignments_to_md,
    page_assignments_to_overview,
    print_page_range_table,
    verify_cover_positions,
)
from xscore.marking.blank_page_detection import check_exam_blank_pages, check_student_handwriting
from xscore.marking.geometry import compute_geometry, write_geometry_artifacts
from xscore.marking.page_order_check import check_page_order
from xscore.pipeline.resume import exam_pdf_page_count
from xscore.scaffold.generate_scaffold import find_exam_pdf
from xscore.shared.exam_paths import (
    artifact_cover_verify_json_path,
    artifact_exam_page_range_overview_path,
    artifact_exam_student_list_json_path,
    artifact_exam_student_list_md_path,
)
from xscore.config import GEMINI_MAX_OUTPUT_TOKENS
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.terminal_ui import (
    announce_step_model,
    format_duration,
    info_line,
    ok_line,
    warn_line,
)


def step_08_cover_empty(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None and ctx.folder is not None
    announce_step_model(
        model_env="EMPTY_EXAM_COVER_MODEL",
        default_model="gemini-2.5-flash",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    exam_pdf = find_exam_pdf(ctx.folder)
    try:
        ctx.empty_exam_has_cover = detect_empty_exam_cover(
            exam_pdf, artifact_dir=ctx.artifact_dir
        )
    except Exception:
        logging.exception("step 8 cover detection failed")
        raise


def step_09_cover_scan_first(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    announce_step_model(
        model_env="COVER_PAGE_DETECTION_MODEL",
        default_model="gemini-2.5-flash",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    ctx.cover_page_mode = detect_first_page_cover(
        ctx.cleaned_pdf,
        artifact_dir=ctx.artifact_dir,
    )


def step_10_geometry(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    exam_pages = ctx.scaffold.page_count if ctx.scaffold else exam_pdf_page_count(ctx.folder)
    try:
        ctx.geo = compute_geometry(
            ctx.cleaned_pdf,
            exam_pages,
            ctx.empty_exam_has_cover,
            ctx.cover_page_mode,
            ctx.students or [],
        )
    except ValueError as exc:
        warn_line(str(exc))
        raise SystemExit(1)
    ctx.num_students = ctx.geo["num_students"]
    ctx.pages_per_student = ctx.geo["pages_per_student"]
    if ctx.geo["roster_mismatch"]:
        n_roster = ctx.geo["num_students_roster"]
        n_scan = ctx.geo["num_students"]
        info_line(f"{n_roster} students in the roster")
        info_line(f"{n_scan} {'student' if n_scan == 1 else 'students'} in the scanned exam")
        if n_scan < n_roster:
            n_absent = n_roster - n_scan
            info_line(
                f"{n_absent} {'student' if n_absent == 1 else 'students'} "
                "sick / did not attend the exam"
            )
        else:
            n_extra = n_scan - n_roster
            info_line(
                f"{n_extra} {'student' if n_extra == 1 else 'students'} "
                "in the scan not on the roster"
            )
    stu_word = "student" if ctx.num_students == 1 else "students"
    ok_line(
        f"{ctx.num_students} {stu_word}  ·  {ctx.pages_per_student} pages each  "
        f"·  {ctx.geo['scan_pages']} scan pages total"
    )
    write_geometry_artifacts(ctx.artifact_dir, ctx.geo)


def step_11_cover_verify(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    if not ctx.cover_page_mode:
        ok_line("Cover-page mode off — verification skipped")
        return
    announce_step_model(
        model_env="COVER_PAGE_DETECTION_MODEL",
        default_model="gemini-2.5-flash",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    cover_ok = verify_cover_positions(
        ctx.cleaned_pdf,
        ctx.pages_per_student,
        ctx.num_students,
        artifact_dir=ctx.artifact_dir,
    )
    json_path = artifact_cover_verify_json_path(ctx.artifact_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps({str(k): v for k, v in cover_ok.items()}, indent=2),
        encoding="utf-8",
    )
    bad = [idx for idx, ok in cover_ok.items() if not ok]
    if not bad:
        return
    msg_lines = [
        f"{len(bad)} expected cover page(s) did not look like cover pages:"
    ]
    for idx in sorted(bad):
        block = idx // ctx.pages_per_student + 1
        msg_lines.append(f"  Block {block} (scan page {idx + 1})  — check scan quality")
    msg_lines.append("  Set COVER_PAGE_VERIFY_STRICT=1 to fail-fast on cover-position mismatches.")
    warn_line("\n".join(msg_lines))
    if os.environ.get("COVER_PAGE_VERIFY_STRICT", "0") == "1":
        raise SystemExit(1)


def step_12_student_names(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    announce_step_model(
        model_env="NAME_DETECTION_MODEL",
        default_model="gemini-2.5-flash",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    t0 = time.perf_counter()
    ctx.page_assignments = assign_pages(
        ctx.cleaned_pdf,
        ctx.students or [],
        pages_per_student=ctx.pages_per_student,
        artifact_dir=ctx.artifact_dir,
        cover_page_mode=ctx.cover_page_mode,
    )
    json_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
    json_path.write_text(page_assignments_to_json(ctx.page_assignments), encoding="utf-8")
    md_path = artifact_exam_student_list_md_path(ctx.artifact_dir)
    md_path.write_text(page_assignments_to_md(ctx.page_assignments), encoding="utf-8")
    overview = page_assignments_to_overview(ctx.page_assignments)
    artifact_exam_page_range_overview_path(ctx.artifact_dir).write_text(overview, encoding="utf-8")
    print_page_range_table(ctx.page_assignments)
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


def step_13_page_order(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None and ctx.folder is not None
    announce_step_model(
        model_env="PAGE_ORDER_CHECK_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_max_tokens=2048,
    )
    from xscore.marking.page_order_check import PageOrderStatus
    t0 = time.perf_counter()
    status, msg = check_page_order(
        find_exam_pdf(ctx.folder),
        ctx.cleaned_pdf,
        ctx.page_assignments,
        artifact_dir=ctx.artifact_dir,
    )
    dur = format_duration(time.perf_counter() - t0)
    n = len(ctx.page_assignments)
    if status is PageOrderStatus.PASSED:
        ok_line(f"Page order check: {n}/{n} students OK  ·  {dur}")
        return
    if status is PageOrderStatus.MISMATCH_FOUND:
        warn_line(msg or "Page order mismatch detected.")
        raise SystemExit(1)
    # INCONCLUSIVE
    warn_line(
        "Page order check INCONCLUSIVE — pipeline did NOT verify page order:\n"
        f"  {msg}\n"
        "  Set PAGE_ORDER_CHECK_STRICT=1 to fail-fast on inconclusive checks."
    )
    if os.environ.get("PAGE_ORDER_CHECK_STRICT", "0") == "1":
        raise SystemExit(1)


def step_14_exam_blank(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None and ctx.folder is not None
    announce_step_model(
        model_env="EXAM_BLANK_DETECTION_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_max_tokens=256,
    )
    from xscore.marking.blank_page_detection import BlankCheckStatus
    t0 = time.perf_counter()
    status, msg = check_exam_blank_pages(
        find_exam_pdf(ctx.folder),
        ctx.artifact_dir,
    )
    dur = format_duration(time.perf_counter() - t0)
    if status is BlankCheckStatus.PASSED:
        ok_line(f"Exam blank detection: {msg}  ·  {dur}")
        return
    # INCONCLUSIVE
    warn_line(
        "Exam blank detection INCONCLUSIVE — pipeline did NOT identify blank exam pages:\n"
        f"  {msg}\n"
        "  Set EXAM_BLANK_DETECTION_STRICT=1 to fail-fast on inconclusive checks."
    )
    if os.environ.get("EXAM_BLANK_DETECTION_STRICT", "0") == "1":
        raise SystemExit(1)


def step_15_handwriting(ctx: _Ctx) -> None:
    assert ctx.cleaned_pdf is not None and ctx.artifact_dir is not None
    announce_step_model(
        model_env="HANDWRITING_CHECK_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_max_tokens=32,
    )
    from xscore.marking.blank_page_detection import BlankCheckStatus
    t0 = time.perf_counter()
    status, msg = check_student_handwriting(
        ctx.cleaned_pdf,
        ctx.page_assignments,
        ctx.artifact_dir,
        empty_exam_has_cover=bool(ctx.empty_exam_has_cover),
    )
    dur = format_duration(time.perf_counter() - t0)
    if status is BlankCheckStatus.PASSED:
        ok_line(f"Student handwriting check: {msg}  ·  {dur}")
        return
    # INCONCLUSIVE
    warn_line(
        "Student handwriting check INCONCLUSIVE — pipeline did NOT verify all blank pages:\n"
        f"  {msg}\n"
        "  Set HANDWRITING_CHECK_STRICT=1 to fail-fast on inconclusive checks."
    )
    if os.environ.get("HANDWRITING_CHECK_STRICT", "0") == "1":
        raise SystemExit(1)
