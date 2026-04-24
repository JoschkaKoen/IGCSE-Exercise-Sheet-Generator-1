"""Assign PDF pages to students by reading names from the top of each page.

Step 10 sub-step of the pipeline:
1. Render each page of the cleaned scan PDF at *dpi*.
2. (Optional) Detect whether the exam uses cover pages by checking scan page 1
   with the ``COVER_PAGE_DETECTION_MODEL``.  If cover-page mode is active,
   every expected cover position is also verified in parallel.
3. Crop the top fraction (name area) and send to the vision model.
4. Fuzzy-match the returned name against the student roster.
5. Group pages into fixed blocks of *pages_per_student* (from geometry).
   Undetected names become ``Unknown_N`` entries rather than inheriting
   from a neighbouring student.

Name detection model: ``NAME_DETECTION_MODEL`` env var (default ``gemini-2.5-flash``).
Cover detection model: ``COVER_PAGE_DETECTION_MODEL`` env var (default ``gemini-2.5-flash``).
Worker count: ``NAME_WORKERS`` env var (default ``min(n_pages, 8)``).

Returns a list of ``PageAssignment`` objects (one per student block).
``PageAssignment.cover_page_number`` is set to the 1-based scan page of the
cover page when cover-page mode is active, or ``None`` otherwise.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from xscore.config import COVER_PAGE_DETECTION_DPI, GEMINI_MAX_OUTPUT_TOKENS, NAME_RECOGNITION_DPI

import json

from .ai_helpers import ai_image_call, page_to_jpeg_b64
from xscore.shared.exam_paths import artifact_prompt_path
from xscore.shared.models import PageAssignment
from xscore.shared.prompt_logger import save_prompt, save_response, save_thinking


_NAME_PROMPT_FREEFORM = """\
Look at the top of this exam page for the student's HANDWRITTEN name.

Ignore all pre-printed or typed text: exam codes, stamps, watermarks, \
school names, barcodes, or labels (e.g. printed codes, page numbers, stamps).

Return ONLY a JSON object:
{"name": "Full name as written"}

If no handwritten name is visible or the name field is blank, return:
{"name": ""}
"""


def _make_name_prompt(students: list[str]) -> str:
    roster = "\n".join(f"  - {s}" for s in students)
    return f"""\
Look at the top of this exam page for the student's HANDWRITTEN name.

Ignore all pre-printed or typed text: exam codes, stamps, watermarks, \
school names, barcodes, or labels (e.g. printed codes, page numbers, stamps).

Here is the official student roster:
{roster}

Return ONLY a JSON object with the roster name that best matches the \
handwritten name, spelled EXACTLY as it appears in the roster above:
{{"name": "Full name as written"}}

If no handwritten name is visible or none of the roster entries match, return:
{{"name": ""}}
"""


_COVER_PAGE_TEXT_PROMPT = """\
A COVER PAGE contains a name/identification field (where the student writes \
their name, class, date, or candidate number) but no exercises or questions.

Here is the text extracted from this exam page:

{text}

Is this page a cover page? Answer true or false.
"""


_ocr_engine = None


def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _ocr_engine = RapidOCR()
    return _ocr_engine


def is_cover_page(
    pdf_path: Path,
    page_idx: int,
    gai_client,
    model_id: str,
    *,
    prompt_save_path: Path | None = None,
    effort: str | None = None,
) -> bool:
    """Cover-page detection for scanned pages via OCR + text-only AI call.

    Renders the page to a grayscale pixmap, runs RapidOCR to extract printed
    text (conf > 0.8 filters out handwriting), then sends the text to the model.
    No temp file, no Gemini Files API upload.
    """
    import fitz
    from google.genai import types as gai_types
    from xscore.shared.terminal_ui import warn_line

    with fitz.open(str(pdf_path)) as doc:
        page = doc[page_idx]
        clip = fitz.Rect(0, 0, page.rect.width, page.rect.height * 0.5)
        pix = page.get_pixmap(dpi=COVER_PAGE_DETECTION_DPI, colorspace=fitz.csGRAY, clip=clip)

    result, _ = _get_ocr()(pix.tobytes("png"))
    printed_text = "\n".join(
        text for _, text, conf in (result or []) if float(conf) > 0.8
    )

    prompt = _COVER_PAGE_TEXT_PROMPT.format(text=printed_text or "(no text extracted)")

    save_prompt(prompt_save_path, model=model_id,
                messages=[{"role": "user", "content": prompt}])

    _thinking_map = {"off": 0, "low": 1024, "high": 8192}
    _budget = _thinking_map.get(effort or "off", 0)
    config = gai_types.GenerateContentConfig(
        max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
        response_mime_type="application/json",
        response_schema=bool,
        thinking_config=gai_types.ThinkingConfig(
            thinking_budget=_budget,
            include_thoughts=_budget > 0,
        ),
    )
    resp = gai_client.models.generate_content(
        model=model_id,
        contents=[gai_types.Part.from_text(text=prompt)],
        config=config,
    )

    thinking_parts: list[str] = []
    answer_parts: list[str] = []
    for candidate in (resp.candidates or []):
        for part in getattr(candidate.content, "parts", None) or []:
            text = part.text or ""
            if getattr(part, "thought", False):
                thinking_parts.append(text)
            else:
                answer_parts.append(text)

    thinking_text = "".join(thinking_parts)
    raw = "".join(answer_parts) or resp.text or ""

    if not raw:
        warn_line(f"[{model_id}] cover page check returned empty response")
    save_thinking(prompt_save_path, thinking_text)
    save_response(prompt_save_path, raw)
    try:
        return bool(json.loads(raw)) if raw else False
    except (json.JSONDecodeError, ValueError):
        return False


def check_cover_page_text(
    pdf_path: Path,
    page_idx: int,
    gai_client,
    model_id: str,
    *,
    prompt_save_path: Path | None = None,
    effort: str | None = None,
) -> bool:
    """Cover-page detection for vector PDFs via text extraction (no vision).

    Extracts page text with fitz and sends it as a plain-text prompt.
    No temp file, no Gemini Files API upload needed.
    """
    import fitz
    from google.genai import types as gai_types
    from xscore.shared.terminal_ui import warn_line

    with fitz.open(str(pdf_path)) as doc:
        page_text = doc[page_idx].get_text().strip()

    prompt = _COVER_PAGE_TEXT_PROMPT.format(text=page_text or "(no text extracted)")

    save_prompt(prompt_save_path, model=model_id,
                messages=[{"role": "user", "content": prompt}])

    _thinking_map = {"off": 0, "low": 1024, "high": 8192}
    _budget = _thinking_map.get(effort or "off", 0)
    config = gai_types.GenerateContentConfig(
        max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
        response_mime_type="application/json",
        response_schema=bool,
        thinking_config=gai_types.ThinkingConfig(
            thinking_budget=_budget,
            include_thoughts=_budget > 0,
        ),
    )
    resp = gai_client.models.generate_content(
        model=model_id,
        contents=[gai_types.Part.from_text(text=prompt)],
        config=config,
    )

    thinking_parts: list[str] = []
    answer_parts: list[str] = []
    for candidate in (resp.candidates or []):
        for part in getattr(candidate.content, "parts", None) or []:
            text = part.text or ""
            if getattr(part, "thought", False):
                thinking_parts.append(text)
            else:
                answer_parts.append(text)

    thinking_text = "".join(thinking_parts)
    raw = "".join(answer_parts) or resp.text or ""

    if not raw:
        warn_line(f"[{model_id}] cover page text check returned empty response")
    save_thinking(prompt_save_path, thinking_text)
    save_response(prompt_save_path, raw)
    try:
        return bool(json.loads(raw)) if raw else False
    except (json.JSONDecodeError, ValueError):
        return False


def _crop_top(page, fraction: float = 0.15):
    """Return the top *fraction* of a PIL image."""
    w, h = page.size
    return page.crop((0, 0, w, int(h * fraction)))


# Paper-size thresholds for name-area cropping.
# A4 long side ≈ 297 mm; A3 long side ≈ 420 mm; threshold at midpoint.
_A3_LONG_SIDE_THRESHOLD_MM: float = 360.0
_NAME_CROP_FRACTION_A4: float = 0.5       # top half
_NAME_CROP_FRACTION_A3: float = 1 / 3    # top third


def _name_crop_fraction(page: Any, dpi: int) -> float:
    """Return the name-area crop fraction for *page* based on its paper size.

    A4 (portrait or landscape) → 0.5 (top half).
    A3 (portrait or landscape) → 1/3 (top third).
    Falls back to A4 for unrecognised sizes.
    """
    w, h = page.size
    long_side_mm = max(w, h) / dpi * 25.4
    return _NAME_CROP_FRACTION_A3 if long_side_mm > _A3_LONG_SIDE_THRESHOLD_MM else _NAME_CROP_FRACTION_A4


def assign_pages(
    cleaned_pdf: Path,
    students: list[str],
    dpi: int = NAME_RECOGNITION_DPI,
    pages_per_student: int = 1,
    name_crop_fraction: float | None = None,
    *,
    pages: list | None = None,
    artifact_dir: Path | None = None,
) -> list[PageAssignment]:
    """Return one ``PageAssignment`` per student block.

    Pages are grouped into fixed blocks of *pages_per_student* (as determined
    by exam geometry). The name is read from the first page of each block.
    Blocks with no detectable name are recorded as ``Unknown_N`` with
    ``confidence="low"`` instead of being merged into a neighbouring student.

    The vision model is configured via ``NAME_DETECTION_MODEL`` (default
    ``qwen3.6-plus, off``). Worker parallelism via ``NAME_WORKERS`` (default
    ``min(n_blocks, 8)``).

    *name_crop_fraction*: fraction of page height to crop for name detection.
    ``None`` (default) auto-detects A4 (top half, 0.5) vs A3 (top third, 1/3)
    from pixel dimensions.  Pass a float to override for all pages.

    *pages*: optional pre-rendered PIL images at *dpi* (skips PDF rendering).
    """
    from xscore.extraction.ground_truth import fuzzy_match_name
    from pdf2image import convert_from_path
    from eXercise.ai_client import make_ai_client, parse_model_effort

    ai_result = make_ai_client(model_env="NAME_DETECTION_MODEL", default_model="gemini-2.5-flash")
    if ai_result is None:
        raise RuntimeError(
            "NAME_DETECTION_MODEL client could not be created — check API key in .env"
        )
    client, model_id, _provider, _effort = ai_result

    from xscore.shared.terminal_ui import info_line, ok_line, tool_line, format_duration, warn_line

    if pages is None:
        tool_line("pages", f"Rendering pages @ {dpi} DPI …")
        pages = convert_from_path(str(cleaned_pdf), dpi=dpi, thread_count=os.cpu_count() or 4)
    n_pages = len(pages)
    import math
    n_blocks = math.ceil(n_pages / pages_per_student)
    first_page_set = {b * pages_per_student + 1 for b in range(n_blocks)}  # 1-based scan pages

    # ------------------------------------------------------------------
    # Cover-page detection (independent of name detection below)
    # Uses COVER_PAGE_DETECTION_MODEL — separate client, separate concern.
    # ------------------------------------------------------------------
    cover_page_mode: bool = False
    cover_ok: dict[int, bool] = {}  # 0-based index → is_cover_page result

    from eXercise.ai_client import make_gemini_native_client
    _gai_client = make_gemini_native_client()
    if _gai_client is None:
        warn_line(
            "GEMINI_API_KEY not set — cover-page detection skipped, running in standard mode"
        )
    else:
        _cover_model, _cover_effort = parse_model_effort(
            os.environ.get("COVER_PAGE_DETECTION_MODEL", "gemini-2.5-flash")
        )

        _p1_save = artifact_prompt_path(artifact_dir, "8_cover_p1") if artifact_dir else None
        _t_cover = time.perf_counter()
        page1_is_cover = is_cover_page(cleaned_pdf, 0, _gai_client, _cover_model, prompt_save_path=_p1_save, effort=_cover_effort)
        _cover_elapsed = format_duration(time.perf_counter() - _t_cover)
        cover_page_mode = page1_is_cover

        if cover_page_mode:
            ok_line(f"Scan page 1: cover page — cover-page mode active  ·  {_cover_elapsed}")

            # Verify every expected cover position in parallel.
            # Block 0 reuses the page-1 result; blocks 1..n-1 are checked in parallel.
            cover_ok[0] = page1_is_cover
            cover_indices_to_check = [
                b * pages_per_student for b in range(1, n_blocks)
            ]

            def _check_cover(idx: int) -> tuple[int, bool]:
                save_path = artifact_prompt_path(artifact_dir, f"8_cover_p{idx + 1}") if artifact_dir else None
                return idx, is_cover_page(cleaned_pdf, idx, _gai_client, _cover_model, prompt_save_path=save_path, effort=_cover_effort)

            if cover_indices_to_check:
                _cover_workers = min(len(cover_indices_to_check), 8)
                _t_verify = time.perf_counter()
                with ThreadPoolExecutor(max_workers=_cover_workers) as ex:
                    for _idx, _ok in ex.map(_check_cover, cover_indices_to_check):
                        cover_ok[_idx] = _ok
                ok_line(
                    f"Verified {len(cover_indices_to_check)} cover position(s)  ·  {format_duration(time.perf_counter() - _t_verify)}"
                )

            # Warn for any block whose expected cover page failed verification.
            for b in range(n_blocks):
                idx = b * pages_per_student
                if not cover_ok.get(idx, False):
                    warn_line(
                        f"Block {b + 1} (scan page {idx + 1}): expected a cover page "
                        f"but this page doesn't look like one — check scan quality"
                    )
        else:
            ok_line(f"Scan page 1: no cover page — standard mode  ·  {_cover_elapsed}")

    # ------------------------------------------------------------------
    # Name detection — only the first page of each student block is checked.
    # In cover-page mode the first page is the cover page, which is where
    # the student writes their name, so the same positions apply.
    # ------------------------------------------------------------------
    info_line("Detecting student names from scan pages …")
    workers = int(os.environ.get("NAME_WORKERS", str(min(n_blocks, 8))))
    prompt = _make_name_prompt(students) if students else _NAME_PROMPT_FREEFORM

    def _ocr_and_match(args: tuple[int, Any]) -> tuple[int, str | None]:
        i, page = args
        fraction = name_crop_fraction if name_crop_fraction is not None else _name_crop_fraction(page, dpi)
        crop = _crop_top(page, fraction=fraction)
        img_b64 = page_to_jpeg_b64(crop)
        save_path = artifact_prompt_path(artifact_dir, f"8_name_{i}") if artifact_dir else None
        _t0 = time.perf_counter()
        raw = ai_image_call(client, img_b64, prompt, max_tokens=64, model_id=model_id,
                              prompt_save_path=save_path, print_latency=False)
        elapsed = time.perf_counter() - _t0
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        raw_name = str(data.get("name", "") or "").strip()
        matched_name = fuzzy_match_name(raw_name, students) if raw_name else None
        ok_line(f"Page {i:3d}/{n_pages}: {raw_name!r}  →  {matched_name!r}  ·  {format_duration(elapsed)}")
        return i, matched_name

    page_results: dict[int, str | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_ocr_and_match, (i, page)): i
            for i, page in enumerate(pages, 1)
            if i in first_page_set
        }
        for fut in as_completed(futures):
            i, matched_name = fut.result()
            page_results[i] = matched_name

    # Restore page order for the block-grouping step below.
    # Non-first pages were not submitted and default to None.
    matched: list[str | None] = [page_results.get(i) for i in range(1, n_pages + 1)]
    del pages  # free PIL image list

    # Group into fixed blocks of pages_per_student (guaranteed by geometry).
    if n_pages % pages_per_student:
        warn_line(
            f"Scan has {n_pages} pages — {n_pages % pages_per_student} trailing page(s) "
            f"dropped (expected a multiple of {pages_per_student})"
        )
    result: list[PageAssignment] = []
    expected_non_cover = pages_per_student - 1  # only meaningful in cover-page mode
    for b in range(n_blocks):
        first_idx = b * pages_per_student           # 0-based index of block's first page
        name = matched[first_idx]
        if name is None:
            name = f"Unknown_{b + 1}"
            confidence = "low"
        else:
            confidence = "high"
        block_pages = list(range(first_idx + 1, first_idx + pages_per_student + 1))  # 1-based

        cover_page_number: int | None = None
        if cover_page_mode:
            cover_page_number = first_idx + 1   # 1-based scan page of this block's cover page
            non_cover_pages = pages_per_student - 1
            if non_cover_pages != expected_non_cover:
                warn_line(
                    f"Student '{name}': expected {expected_non_cover} answer pages, "
                    f"got {non_cover_pages}"
                )

        result.append(PageAssignment(
            student_name=name,
            page_numbers=block_pages,
            confidence=confidence,
            cover_page_number=cover_page_number,
        ))
    return result


def page_assignments_to_json(assignments: list[PageAssignment]) -> str:
    """Serialise a PageAssignment list to a JSON string."""
    import json

    rows = []
    for a in assignments:
        row: dict = {
            "student_name": a.student_name,
            "page_numbers": a.page_numbers,
            "confidence": a.confidence,
        }
        if a.cover_page_number is not None:
            row["cover_page_number"] = a.cover_page_number
        rows.append(row)
    return json.dumps(rows, indent=2, ensure_ascii=False)


def page_assignments_to_md(assignments: list[PageAssignment]) -> str:
    """Return a markdown table of student → scan pages."""
    has_cover = any(a.cover_page_number is not None for a in assignments)
    if has_cover:
        lines = [
            "# Exam Student List (scan-detected)\n",
            "| # | Student | Pages | Confidence | Cover pg |",
            "|---|---------|-------|------------|----------|",
        ]
        for i, a in enumerate(assignments, 1):
            pages = ", ".join(str(p) for p in a.page_numbers)
            cover = str(a.cover_page_number) if a.cover_page_number is not None else "—"
            lines.append(f"| {i} | {a.student_name} | {pages} | {a.confidence} | {cover} |")
    else:
        lines = [
            "# Exam Student List (scan-detected)\n",
            "| # | Student | Pages | Confidence |",
            "|---|---------|-------|------------|",
        ]
        for i, a in enumerate(assignments, 1):
            pages = ", ".join(str(p) for p in a.page_numbers)
            lines.append(f"| {i} | {a.student_name} | {pages} | {a.confidence} |")
    return "\n".join(lines) + "\n"
