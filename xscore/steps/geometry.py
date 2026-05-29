"""Geometry & validation step bodies: cover-page detection, scan geometry,
student name OCR, validations.

Cover detection runs in two phases. ``cover_page_scan_first`` checks scan
page 1 only and sets ``ctx.cover_page_mode``; ``exam_geometry`` then derives
``pages_per_student`` deterministically from that flag and aborts on any
total-page mismatch.

The handwriting / names / page-order checks return ``(status, message)``
from their helpers; the dispatchers below own the policy. INCONCLUSIVE → loud
warn + continue (or ``SystemExit(1)`` when the per-step ``*_STRICT=1`` env
var is set). MISMATCH_FOUND in the handwriting check still raises
``SystemExit(1)``.
"""

from __future__ import annotations

import logging
import os
import time

from xscore.preprocessing.assign_pages_to_students import (
    assign_pages,
    page_assignments_to_json,
    page_assignments_to_md,
    page_assignments_to_overview,
    print_page_range_table,
)
from xscore.preprocessing.cover_detection import (
    detect_empty_exam_cover,
    detect_first_page_cover,
)
from xscore.marking.blank_page_detection import (
    PAGE_TYPE_VOCABULARY,
    check_student_handwriting,
    classify_empty_exam_pages as _classify_empty_exam_pages,
)
from xscore.marking.geometry import compute_geometry, write_geometry_artifacts
from xscore.marking.page_order_check import check_page_order
from xscore.pipeline.resume import exam_pdf_page_count
from xscore.scaffold.generate_scaffold import find_answer_pdf, find_exam_pdf
from xscore.shared.exam_paths import (
    artifact_exam_page_range_overview_path,
    artifact_exam_student_list_json_path,
    artifact_exam_student_list_md_path,
    artifact_subject_dir,
    artifact_subject_json_path,
    artifact_subject_md_path,
    artifact_subject_prompt_path,
)
from xscore.config import GEMINI_MAX_OUTPUT_TOKENS
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.terminal_ui import (
    announce_step_model,
    blank_line,
    confirm_continue,
    format_duration,
    info_line,
    ok_line,
    warn_line,
)


def cover_page_empty_exam(ctx: _Ctx) -> None:
    if ctx.artifact_dir is None or ctx.folder is None:
        raise RuntimeError('invariant failed: ctx.artifact_dir is not None and ctx.folder is not None')
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
        logging.exception("cover_page_empty_exam detection failed")
        raise


def cover_page_scan_first(ctx: _Ctx) -> None:
    if ctx.cleaned_pdf is None or ctx.artifact_dir is None:
        raise RuntimeError('invariant failed: ctx.cleaned_pdf is not None and ctx.artifact_dir is not None')
    announce_step_model(
        model_env="COVER_PAGE_DETECTION_MODEL",
        default_model="gemini-2.5-flash",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    ctx.cover_page_mode = detect_first_page_cover(
        ctx.cleaned_pdf,
        artifact_dir=ctx.artifact_dir,
    )


def exam_geometry(ctx: _Ctx) -> None:
    if ctx.cleaned_pdf is None or ctx.artifact_dir is None:
        raise RuntimeError('invariant failed: ctx.cleaned_pdf is not None and ctx.artifact_dir is not None')
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
    if ctx.geo.get("mismatch_warning"):
        warn_line(ctx.geo["mismatch_warning"])
        if os.environ.get("GEOMETRY_STRICT", "0") == "1":
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


def detect_subject(ctx: _Ctx) -> None:
    """Two-tier subject detection: filename heuristic first, AI fallback.

    Sets ``ctx.subject`` and writes ``11_detect_subject/subject.{json,md}``.
    Gates the ``CODE_FORMATTING`` prompt section in extract_exam_question_numbers,
    extract_exam_questions, parse_mark_scheme, ai_marking, extract_student_answers
    via :func:`xscore.shared.subjects.needs_code_formatting`.
    """
    import json
    from pathlib import Path

    from xscore.shared.subjects import (
        Subject,
        available_subjects_from_env,
        detect_subject_from_filenames,
        get_subject,
    )

    if ctx.artifact_dir is None or ctx.folder is None:
        raise RuntimeError('invariant failed: ctx.artifact_dir is not None and ctx.folder is not None')

    available = available_subjects_from_env()
    if not available:
        raise RuntimeError("AVAILABLE_SUBJECTS is empty; configure it in default.env")

    try:
        exam_pdf: Path | None = find_exam_pdf(ctx.folder)
    except FileNotFoundError:
        exam_pdf = None
    answer_pdf = find_answer_pdf(ctx.folder)

    matched = detect_subject_from_filenames((exam_pdf, answer_pdf), candidates=available)
    ai_meta: dict | None = None
    if matched is not None:
        ctx.subject = matched
        method = "filename"
        ok_line(f"Subject: {matched.name}  (matched filename)")
    else:
        # Only announce the model when an actual API call will follow —
        # filename match short-circuits before any AI traffic.
        announce_step_model(
            model_env="SUBJECT_DETECTION_MODEL",
            default_model="gemini-3.1-flash-lite",
            default_max_tokens=256,
        )
        from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415
        announce_ai_input(kind="PDF", note="Gemini, native bytes")
        ctx.subject, ai_meta = _detect_subject_via_ai(ctx, available, exam_pdf)
        method = "ai"
        ok_line(f"Subject: {ctx.subject.name}  (Gemini classification)")

    artifact_subject_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "name": ctx.subject.name,
        "slug": ctx.subject.slug,
        "needs_code_formatting": ctx.subject.needs_code_formatting,
        "detection_method": method,
    }
    if ai_meta is not None:
        payload["ai"] = ai_meta
    artifact_subject_json_path(ctx.artifact_dir).write_text(
        json.dumps(payload, indent=2) + "\n", encoding="utf-8",
    )
    artifact_subject_md_path(ctx.artifact_dir).write_text(
        f"# Subject\n\n**{ctx.subject.name}**  (slug: `{ctx.subject.slug}`, "
        f"code formatting: {ctx.subject.needs_code_formatting})\n\n"
        f"_Detected via: {method}_\n",
        encoding="utf-8",
    )


def _detect_subject_via_ai(
    ctx: _Ctx,
    available: "tuple",
    exam_pdf,
):
    """AI fallback: extract first 2 pages of the empty exam, classify via Gemini."""
    import json
    from pathlib import Path

    import fitz
    from google.genai import types as gai_types

    from eXercise.ai_client import (
        build_gemini_thinking_config,
        gemini_pdf_part,
        make_gemini_native_client,
        parse_model_spec,
        split_gemini_response,
    )
    from eXercise.api_retry import retry_api_call
    from xscore.shared.prompt_logger import attachment_part, save_prompt, save_response
    from xscore.shared.subjects import get_subject

    src_pdf = (
        ctx.scaffold_state.actual_exam_pdf
        if ctx.scaffold_state is not None else None
    ) or exam_pdf
    if src_pdf is None:
        raise RuntimeError("No empty-exam PDF available for AI subject detection")

    from xscore.shared.prompt_logger import _save_images_enabled

    if _save_images_enabled():
        subject_dir = artifact_subject_dir(ctx.artifact_dir)
        subject_dir.mkdir(parents=True, exist_ok=True)
        preview_pdf = subject_dir / "preview_first_pages.pdf"
        _preview_is_tmp = False
    else:
        import tempfile
        _tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        _tmp.close()
        preview_pdf = Path(_tmp.name)
        _preview_is_tmp = True

    try:
        doc_in = fitz.open(str(src_pdf))
        doc_out = fitz.open()
        try:
            last = min(2, doc_in.page_count) - 1
            doc_out.insert_pdf(doc_in, from_page=0, to_page=last)
            doc_out.save(str(preview_pdf), garbage=4, deflate=True)
        finally:
            doc_in.close()
            doc_out.close()

        model_spec = os.environ.get(
            "SUBJECT_DETECTION_MODEL", "gemini-3.1-flash-lite, 0, 256",
        )
        model, thinking_tokens, max_tokens = parse_model_spec(model_spec)

        from xscore.shared.response_cache import reuse_cache_enabled  # noqa: PLC0415
        client = make_gemini_native_client(should_cache=reuse_cache_enabled(ctx))
        if client is None:
            raise RuntimeError(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) not set — required by detect_subject "
                "AI fallback. Set the env var, or add a filename pattern to "
                "xscore/shared/subjects.py:KNOWN_SUBJECTS so the heuristic matches."
            )

        subject_names = [s.name for s in available]
        system_prompt = (
            "You are classifying an exam paper by its academic subject. "
            f"Choose exactly one subject from this list: {', '.join(subject_names)}."
        )
        user_prompt = (
            "Look at the exam cover page and page 2. Identify the subject from the "
            "subject heading, paper title, and question content. Reply with one of "
            f"the allowed subjects: {', '.join(subject_names)}."
        )

        config_kwargs: dict = {
            "max_output_tokens": max_tokens or 256,
            "response_mime_type": "application/json",
            "response_schema": {
                "type": "object",
                "properties": {"subject": {"type": "string", "enum": subject_names}},
                "required": ["subject"],
            },
        }
        if thinking_tokens is not None:
            config_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
        gen_config = gai_types.GenerateContentConfig(**config_kwargs)

        contents = [
            gemini_pdf_part(client, preview_pdf, label="subject detection"),
            gai_types.Part.from_text(text=user_prompt),
        ]
        response = retry_api_call(
            lambda: client.models.generate_content(
                model=model, contents=contents, config=gen_config,
            ),
            label="Subject Detection",
        )
        answer_text, thinking_text = split_gemini_response(response)
        detected_name = json.loads(answer_text)["subject"]
        subject = get_subject(detected_name)

        prompt_path = artifact_subject_prompt_path(ctx.artifact_dir, "subject")
        save_prompt(
            prompt_path,
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    attachment_part(preview_pdf.read_bytes(), "application/pdf"),
                    {"type": "text", "text": user_prompt},
                ]},
            ],
        )
        save_response(prompt_path, answer_text, thinking=thinking_text)

        return subject, {"model": model, "raw_response": answer_text}
    finally:
        if _preview_is_tmp:
            try:
                preview_pdf.unlink()
            except OSError:
                pass


def student_names(ctx: _Ctx) -> None:
    if ctx.cleaned_pdf is None or ctx.artifact_dir is None:
        raise RuntimeError('invariant failed: ctx.cleaned_pdf is not None and ctx.artifact_dir is not None')
    announce_step_model(
        model_env="NAME_DETECTION_MODEL",
        default_model="gemini-2.5-flash",
        default_max_tokens=GEMINI_MAX_OUTPUT_TOKENS,
    )
    from xscore.config import NAME_JPEG_QUALITY, NAME_RECOGNITION_DPI  # noqa: PLC0415
    from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415
    announce_ai_input(
        kind="JPEG", dpi=NAME_RECOGNITION_DPI, quality=NAME_JPEG_QUALITY,
        note="name-band crop",
    )
    t0 = time.perf_counter()
    from xscore.shared.response_cache import reuse_cache_enabled  # noqa: PLC0415
    ctx.page_assignments = assign_pages(
        ctx.cleaned_pdf,
        ctx.students or [],
        pages_per_student=ctx.pages_per_student,
        artifact_dir=ctx.artifact_dir,
        cover_page_mode=ctx.cover_page_mode,
        should_cache=reuse_cache_enabled(ctx),
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


def page_order_check(ctx: _Ctx) -> None:
    """Heuristic page-order check (no LLM, no OCR).

    Reads ``student_handwriting_check``'s ``handwriting.json`` and verifies
    each student's detected page numbers form the expected sequence given
    the empty-exam layout. On mismatches, renders detail tables and asks
    the user whether to continue.
    """
    if ctx.cleaned_pdf is None or ctx.artifact_dir is None or ctx.folder is None:
        raise RuntimeError('invariant failed: ctx.cleaned_pdf is not None and ctx.artifact_dir is not None and (ctx.folder is not None)')
    from xscore.marking.page_order_check import PageOrderStatus, render_problem_tables
    t0 = time.perf_counter()
    result = check_page_order(
        find_exam_pdf(ctx.folder),
        ctx.cleaned_pdf,
        ctx.page_assignments,
        artifact_dir=ctx.artifact_dir,
    )
    dur = format_duration(time.perf_counter() - t0)
    n = result.total_count

    if result.status is PageOrderStatus.PASSED:
        ok_line(f"Page order check: {n}/{n} students OK  ·  {dur}")
        return

    if result.status is PageOrderStatus.INCONCLUSIVE:
        warn_line(
            "Page order check INCONCLUSIVE — pipeline did NOT verify page order:\n"
            f"  {result.setup_error}\n"
            "  Set PAGE_ORDER_CHECK_STRICT=1 to fail-fast on inconclusive checks."
        )
        if os.environ.get("PAGE_ORDER_CHECK_STRICT", "0") == "1":
            raise SystemExit(1)
        return

    # MISMATCH_FOUND — tables + prompt
    render_problem_tables(result)
    if confirm_continue("Continue despite page-order issues?"):
        info_line("Continuing past page-order issues.")
        return
    info_line("Aborted by user.")
    raise SystemExit(1)


def classify_empty_exam_pages(ctx: _Ctx) -> None:
    """Step classify_empty_exam_pages — vision-classify each page of the empty exam paper.

    For each page in the post-cut empty exam, picks a ``page_type`` from the
    closed vocabulary {cover/instruction/question/blank/writing-space page}
    and reads its printed page number. Builds the catalog that student_handwriting_check
    (student_handwriting_check) uses as its matching vocabulary, and that
    detect_cross_page_context (detect_cross_page_context) uses to decide which scan pages are
    continuation pages.

    Writes ``12_classify_empty_exam_pages/empty_exam_classifications.json``.
    """
    import json as _json

    from eXercise.ai_client import parse_model_spec
    from xscore.marking.blank_page_detection import (
        BlankCheckStatus,
        HANDWRITING_JPEG_DPI,
        HANDWRITING_JPEG_QUALITY,
    )
    from xscore.shared.exam_paths import artifact_empty_exam_classifications_json_path
    from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415

    if ctx.artifact_dir is None:
        raise RuntimeError('invariant failed: ctx.artifact_dir is not None')
    if ctx.scaffold_state is None or ctx.scaffold_state.actual_exam_pdf is None:
        raise RuntimeError('classify_empty_exam_pages needs the post-cut empty exam PDF (set by cut_exam_pdf cut_exam_pdf)')

    announce_step_model(
        model_env="EMPTY_EXAM_PAGE_CLASSIFICATION_MODEL",
        legacy_model_env="HANDWRITING_CHECK_MODEL",
        default_max_tokens=256,
    )
    model_id, thinking, max_tok = parse_model_spec(
        os.environ.get(
            "EMPTY_EXAM_PAGE_CLASSIFICATION_MODEL",
            "gemini-3.5-flash, 0, 256",
        )
    )
    if model_id.startswith("gemini"):
        announce_ai_input(kind="PDF", note="native per-page slice")
    else:
        announce_ai_input(
            kind="JPEG", dpi=HANDWRITING_JPEG_DPI, quality=HANDWRITING_JPEG_QUALITY,
            note="rasterized fallback",
        )

    if ctx.scaffold_state is None:
        raise RuntimeError('scaffold_setup must run before classify_empty_exam_pages')
    empty_pdf = ctx.scaffold_state.actual_exam_pdf
    import fitz as _fitz  # noqa: PLC0415
    with _fitz.open(str(empty_pdf)) as _d:
        n_empty = _d.page_count
    info_line(f"Classifying {n_empty} empty-exam pages …")

    t0 = time.perf_counter()
    status, msg, classifications = _classify_empty_exam_pages(
        empty_pdf, ctx.artifact_dir,
        model_id=model_id, thinking_tokens=thinking, max_tokens=max_tok,
    )
    dur = format_duration(time.perf_counter() - t0)

    out_path = artifact_empty_exam_classifications_json_path(ctx.artifact_dir)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _json.dumps({"empty_exam_pages": classifications}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    if status is BlankCheckStatus.PASSED:
        ok_line(f"Empty-exam classification: {msg}  ·  {dur}")
    else:
        warn_line(f"Empty-exam classification INCONCLUSIVE: {msg}  ·  {dur}")

    detected_types = [
        t for t in PAGE_TYPE_VOCABULARY
        if any(c["page_type"] == t for c in classifications)
    ]
    detected_numbers = sorted({
        c["page_number"] for c in classifications
        if c["page_number"] is not None
    })
    types_str = ", ".join(detected_types) if detected_types else "none"
    if not detected_numbers:
        range_str = "none"
    elif detected_numbers[0] == detected_numbers[-1]:
        range_str = str(detected_numbers[0])
    else:
        range_str = f"{detected_numbers[0]}–{detected_numbers[-1]}"
    info_line(
        "In empty exam:\n"
        f"  page types detected: {types_str}\n"
        f"  page number range detected: {range_str}"
    )
    blank_line()


def student_handwriting_check(ctx: _Ctx) -> None:
    """Step student_handwriting_check — per-scan-page closed-vocabulary matcher.

    Loads the catalog written by classify_empty_exam_pages and asks the vision LLM to MATCH
    each scan page against it (page type + page number, plus an N+3 overflow
    buffer). Detects student handwriting in the same call. The output lives
    in ``13_student_handwriting_check/handwriting.json``.
    """
    import json as _json

    from xscore.marking.blank_page_detection import BlankCheckStatus  # noqa: PLC0415
    from xscore.config import (  # noqa: PLC0415
        HANDWRITING_CHECK_JPEG_DPI,
        HANDWRITING_CHECK_JPEG_QUALITY,
    )
    from xscore.marking.marking_page_register import _cover_offset
    from xscore.shared.exam_paths import (
        artifact_empty_exam_classifications_json_path,
        artifact_handwriting_json_path,
    )
    from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415

    if ctx.cleaned_pdf is None or ctx.artifact_dir is None:
        raise RuntimeError('invariant failed: ctx.cleaned_pdf is not None and ctx.artifact_dir is not None')
    if not (ctx.pages_per_student is not None and ctx.pages_per_student > 0):
        raise RuntimeError('invariant failed: ctx.pages_per_student is not None and ctx.pages_per_student > 0')

    classifications_path = artifact_empty_exam_classifications_json_path(ctx.artifact_dir)
    if classifications_path.is_file():
        empty_classifications = _json.loads(
            classifications_path.read_text(encoding="utf-8")
        ).get("empty_exam_pages", [])
    else:
        # Legacy fallback: pre-classify_empty_exam_pages-split runs wrote the catalog into
        # 14_student_handwriting/handwriting.json. Read it from there if
        # the new artifact is missing.
        legacy_path = artifact_handwriting_json_path(ctx.artifact_dir)
        if legacy_path.is_file():
            empty_classifications = _json.loads(
                legacy_path.read_text(encoding="utf-8")
            ).get("empty_exam_pages", [])
        else:
            warn_line(
                "student_handwriting_check: no empty-exam classifications found — running with empty "
                "catalog (matcher will fall back to its always-available cover-page "
                "vocabulary + page-number overflow buffer)."
            )
            empty_classifications = []

    announce_step_model(
        model_env="HANDWRITING_CHECK_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_max_tokens=192,
    )
    announce_ai_input(
        kind="JPEG",
        dpi=HANDWRITING_CHECK_JPEG_DPI,
        quality=HANDWRITING_CHECK_JPEG_QUALITY,
    )
    cover_page_mode = bool(ctx.cover_page_mode)
    cover_offset = _cover_offset(cover_page_mode, bool(ctx.empty_exam_has_cover))
    t0 = time.perf_counter()
    empty_exam_pdf = (
        ctx.scaffold_state.actual_exam_pdf
        if ctx.scaffold_state is not None else None
    )
    status, msg = check_student_handwriting(
        ctx.cleaned_pdf,
        ctx.artifact_dir,
        cover_page_mode=cover_page_mode,
        pages_per_student=ctx.pages_per_student,
        cover_offset=cover_offset,
        empty_exam_classifications=empty_classifications,
        empty_exam_pdf=empty_exam_pdf,
    )
    dur = format_duration(time.perf_counter() - t0)

    if status is BlankCheckStatus.PASSED:
        ok_line(f"Scan-page matching: {msg}  ·  {dur}")
        return
    warn_line(
        "Scan-page matching INCONCLUSIVE — pipeline did NOT verify all blank pages:\n"
        f"  {msg}"
    )


def build_marking_register_v1(ctx: _Ctx) -> None:
    """Build and persist the v1 marking page register.

    Pure data transform — combines student_handwriting_check (per-scan-page
    handwriting flags) with student_names (page_assignments) and
    ``ctx.empty_exam_has_cover``. Each non-cover scan page with handwriting
    becomes one primary marking call. Continuation-page attachment (blank or
    writing-space pages with handwriting → attach to the previous question
    page) is applied later by detect_cross_page_context (detect_cross_page_context).
    """
    if ctx.artifact_dir is None:
        raise RuntimeError('invariant failed: ctx.artifact_dir is not None')
    from xscore.marking.marking_page_register import (
        build_initial_register, write_register,
    )
    from xscore.shared.path_builders import artifact_marking_page_register_v1_path
    t0 = time.perf_counter()
    register = build_initial_register(ctx)
    write_register(
        artifact_marking_page_register_v1_path(ctx.artifact_dir), register
    )
    dur = format_duration(time.perf_counter() - t0)
    n_students = len(register.get("students", []))
    n_calls = register.get("metadata", {}).get("total_calls", 0)
    ok_line(
        f"Marking page register: {n_students} students, "
        f"{n_calls} marking calls  ·  {dur}"
    )
