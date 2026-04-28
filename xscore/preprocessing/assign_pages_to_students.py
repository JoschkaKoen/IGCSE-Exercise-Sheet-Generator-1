"""Assign PDF pages to students by reading names from the top of each page.

Steps 9, 11, and 12 of the pipeline:
- Step  9: ``detect_first_page_cover`` — checks scan page 1 for a cover page.
- Step 11: ``verify_cover_positions`` — verifies covers on remaining students
  in parallel (only runs when step 9 said yes).
- Step 12: ``assign_pages`` — renders pages, reads student names, groups
  into blocks of ``pages_per_student``.

Name detection model: ``NAME_DETECTION_MODEL`` env var (default ``gemini-2.5-flash``).
Cover detection model: ``COVER_PAGE_DETECTION_MODEL`` env var (default ``gemini-2.5-flash``).
Worker count: ``NAME_WORKERS`` env var (default ``min(n_pages, 8)``).

Returns a list of ``PageAssignment`` objects (one per student block).
``PageAssignment.cover_page_number`` is set to the 1-based scan page of the
cover page when cover-page mode is active, or ``None`` otherwise.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from xscore.config import COVER_PAGE_DETECTION_DPI, GEMINI_MAX_OUTPUT_TOKENS, NAME_RECOGNITION_DPI

from eXercise.api_retry import retry_api_call
from xscore.marking.ai_helpers import ai_image_call, page_to_jpeg_b64
from xscore.prompts.loader import load_prompt
from xscore.shared.exam_paths import (
    artifact_cover_page_dir,
    artifact_cover_scan_prompt_path,
    artifact_cover_verify_prompt_path,
    artifact_names_prompt_path,
)
from xscore.shared.models import PageAssignment
from xscore.shared.prompt_logger import save_prompt, save_response


def _make_name_prompt(students: list[str]) -> str:
    roster = "\n".join(f"  - {s}" for s in students)
    return load_prompt("student_names_with_roster", roster=roster)[1]


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
    thinking_tokens: int | None = None,
    max_tokens: int | None = None,
) -> bool:
    """Cover-page detection for scanned pages via OCR + text-only AI call.

    Renders the page to a grayscale pixmap, runs RapidOCR to extract printed
    text (conf > 0.8 filters out handwriting), then sends the text to the model.
    No temp file, no Gemini Files API upload. Routes to the right provider based
    on the resolved model name; *gai_client* is used only on the Gemini branch.
    """
    import fitz
    from xscore.shared.terminal_ui import warn_line

    with fitz.open(str(pdf_path)) as doc:
        page = doc[page_idx]
        clip = fitz.Rect(0, 0, page.rect.width, page.rect.height * 0.5)
        pix = page.get_pixmap(dpi=COVER_PAGE_DETECTION_DPI, colorspace=fitz.csGRAY, clip=clip)

    result, _ = _get_ocr()(pix.tobytes("png"))
    printed_text = "\n".join(
        text for _, text, conf in (result or []) if float(conf) > 0.8
    )

    prompt = load_prompt("cover_page_scan", text=printed_text or "(no text extracted)")[1]

    save_prompt(prompt_save_path, model=model_id,
                messages=[{"role": "user", "content": prompt}])

    thinking_text = ""
    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import build_gemini_thinking_config
        config = gai_types.GenerateContentConfig(
            max_output_tokens=max_tokens or GEMINI_MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
            response_schema=bool,
            thinking_config=build_gemini_thinking_config(thinking_tokens),
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
    else:
        from eXercise.ai_client import (
            build_completion_kwargs,
            collect_streamed_response,
            make_ai_client,
        )
        _result = make_ai_client(model_env="COVER_PAGE_DETECTION_MODEL")
        if _result is None:
            warn_line(
                f"COVER_PAGE_DETECTION_MODEL={model_id} requires API key — "
                f"treating page as non-cover"
            )
            return False
        _oa_client, _, _provider, _, _ = _result
        _use_stream, _kw = build_completion_kwargs(_provider, thinking_tokens, max_tokens)
        _oa_prompt = prompt + '\n\nReturn JSON only with this shape: {"answer": <bool>}'
        _msgs = [{"role": "user", "content": _oa_prompt}]
        if _use_stream:
            _th: list[str] = []
            _stream = _oa_client.chat.completions.create(
                model=model_id, messages=_msgs, stream=True, **_kw,
            )
            raw = collect_streamed_response(_stream, thinking_out=_th)
            thinking_text = "".join(_th)
        else:
            try:
                _resp = _oa_client.chat.completions.create(
                    model=model_id, messages=_msgs,
                    response_format={"type": "json_object"}, **_kw,
                )
            except Exception:
                _resp = _oa_client.chat.completions.create(
                    model=model_id, messages=_msgs, **_kw,
                )
            raw = _resp.choices[0].message.content or ""
            thinking_text = getattr(_resp.choices[0].message, "reasoning_content", "") or ""

    if not raw:
        warn_line(f"[{model_id}] cover page check returned empty response")
    save_response(prompt_save_path, raw, thinking=thinking_text)
    return _parse_cover_bool(raw)


def _parse_cover_bool(raw: str) -> bool:
    """Parse the model response for cover-page detection.

    Tolerates both Gemini's bare ``true``/``false`` (from ``response_schema=bool``)
    and the OpenAI-compat ``{"answer": <bool>}`` shape.
    """
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return False
    if isinstance(data, bool):
        return data
    if isinstance(data, dict):
        return bool(data.get("answer", data.get("is_cover_page", False)))
    return False


def check_cover_page_text(
    pdf_path: Path,
    page_idx: int,
    gai_client,
    model_id: str,
    *,
    prompt_save_path: Path | None = None,
    thinking_tokens: int | None = None,
    max_tokens: int | None = None,
) -> bool:
    """Cover-page detection for vector PDFs via text extraction (no vision).

    Extracts page text with fitz and sends it as a plain-text prompt.
    No temp file, no Gemini Files API upload needed. Routes to the right
    provider based on the resolved model name; *gai_client* is used only on
    the Gemini branch.
    """
    import fitz
    from xscore.shared.terminal_ui import warn_line

    with fitz.open(str(pdf_path)) as doc:
        page_text = doc[page_idx].get_text().strip()

    prompt = load_prompt("cover_page_scan", text=page_text or "(no text extracted)")[1]

    save_prompt(prompt_save_path, model=model_id,
                messages=[{"role": "user", "content": prompt}])

    thinking_text = ""
    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import build_gemini_thinking_config
        config = gai_types.GenerateContentConfig(
            max_output_tokens=max_tokens or GEMINI_MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
            response_schema=bool,
            thinking_config=build_gemini_thinking_config(thinking_tokens),
        )
        resp = retry_api_call(
            lambda: gai_client.models.generate_content(
                model=model_id,
                contents=[gai_types.Part.from_text(text=prompt)],
                config=config,
            ),
            label=f"Cover page check ({model_id})",
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
    else:
        from eXercise.ai_client import (
            build_completion_kwargs,
            collect_streamed_response,
            make_ai_client,
        )
        _result = make_ai_client(model_env="EMPTY_EXAM_COVER_MODEL")
        if _result is None:
            warn_line(
                f"EMPTY_EXAM_COVER_MODEL={model_id} requires API key — "
                f"treating page as non-cover"
            )
            return False
        _oa_client, _, _provider, _, _ = _result
        _use_stream, _kw = build_completion_kwargs(_provider, thinking_tokens, max_tokens)
        _oa_prompt = prompt + '\n\nReturn JSON only with this shape: {"answer": <bool>}'
        _msgs = [{"role": "user", "content": _oa_prompt}]
        if _use_stream:
            def _do_stream() -> tuple[str, str]:
                _th: list[str] = []
                _stream = _oa_client.chat.completions.create(
                    model=model_id, messages=_msgs, stream=True, **_kw,
                )
                _raw = collect_streamed_response(_stream, thinking_out=_th)
                return _raw, "".join(_th)

            raw, thinking_text = retry_api_call(
                _do_stream, label=f"Cover page check ({model_id}, stream)",
            )
        else:
            def _do_json() -> tuple[str, str]:
                _resp = _oa_client.chat.completions.create(
                    model=model_id, messages=_msgs,
                    response_format={"type": "json_object"}, **_kw,
                )
                return (
                    _resp.choices[0].message.content or "",
                    getattr(_resp.choices[0].message, "reasoning_content", "") or "",
                )

            def _do_plain() -> tuple[str, str]:
                _resp = _oa_client.chat.completions.create(
                    model=model_id, messages=_msgs, **_kw,
                )
                return (
                    _resp.choices[0].message.content or "",
                    getattr(_resp.choices[0].message, "reasoning_content", "") or "",
                )

            try:
                raw, thinking_text = retry_api_call(
                    _do_json, label=f"Cover page check ({model_id}, json)",
                )
            except Exception:
                raw, thinking_text = retry_api_call(
                    _do_plain, label=f"Cover page check ({model_id}, plain)",
                )

    if not raw:
        warn_line(f"[{model_id}] cover page text check returned empty response")
    save_response(prompt_save_path, raw, thinking=thinking_text)
    return _parse_cover_bool(raw)


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


def detect_empty_exam_cover(
    exam_pdf: Path,
    *,
    artifact_dir: Path | None = None,
) -> bool | None:
    """Step 8 — Check whether page 1 of the empty exam PDF is a cover page.

    Returns True/False on success, or None when skipped (no API key, missing
    google-genai, etc.). The None case is meaningful: ``compute_geometry``
    raises on (empty=None, scan=True) so we never silently default the cover
    offset when step 8 didn't produce a value.
    """
    from eXercise.ai_client import parse_model_spec
    from xscore.shared.terminal_ui import ok_line, warn_line, format_duration

    model, thinking_tokens, max_tokens = parse_model_spec(
        os.environ.get("EMPTY_EXAM_COVER_MODEL", "gemini-2.5-flash")
    )
    gai_client = None
    if model.startswith("gemini"):
        from eXercise.ai_client import make_gemini_native_client  # noqa: PLC0415
        gai_client = make_gemini_native_client()
        if gai_client is None:
            warn_line("Empty exam cover check skipped — no GEMINI_API_KEY")
            return None

    save_path = None
    if artifact_dir is not None:
        save_dir = artifact_cover_page_dir(artifact_dir)
        save_dir.mkdir(parents=True, exist_ok=True)
        save_path = save_dir / "cover_empty_exam_prompt.md"

    t0 = time.perf_counter()
    has_cover = check_cover_page_text(
        exam_pdf, 0, gai_client, model,
        prompt_save_path=save_path,
        thinking_tokens=thinking_tokens,
        max_tokens=max_tokens,
    )
    ok_line(
        f"Empty exam page 1: {'cover page' if has_cover else 'no cover page'}"
        f"  ·  {format_duration(time.perf_counter() - t0)}"
    )
    return has_cover


def detect_first_page_cover(
    cleaned_pdf: Path,
    *,
    artifact_dir: Path | None = None,
) -> bool:
    """Step 9 — Check whether scan page 1 is a cover page.

    Returns True if the first scan page looks like a cover page (and the
    scan therefore uses cover-page mode). Returns False if not, or if API
    access is unavailable (treated as standard mode).
    """
    from eXercise.ai_client import make_gemini_native_client, parse_model_spec
    from xscore.shared.terminal_ui import ok_line, warn_line, format_duration

    model, thinking, max_tokens = parse_model_spec(
        os.environ.get("COVER_PAGE_DETECTION_MODEL", "gemini-2.5-flash")
    )
    if model.startswith("gemini"):
        gai_client = make_gemini_native_client()
        if gai_client is None:
            warn_line(
                "GEMINI_API_KEY not set — cover-page detection skipped, running in standard mode"
            )
            return False
    else:
        gai_client = None

    save_path = artifact_cover_scan_prompt_path(artifact_dir, "cover_p1") if artifact_dir else None
    t0 = time.perf_counter()
    page1_is_cover = is_cover_page(
        cleaned_pdf, 0, gai_client, model,
        prompt_save_path=save_path,
        thinking_tokens=thinking, max_tokens=max_tokens,
    )
    elapsed = format_duration(time.perf_counter() - t0)
    if page1_is_cover:
        ok_line(f"Scan page 1: cover page — cover-page mode active  ·  {elapsed}")
    else:
        ok_line(f"Scan page 1: no cover page — standard mode  ·  {elapsed}")
    return page1_is_cover


def verify_cover_positions(
    cleaned_pdf: Path,
    pages_per_student: int,
    num_students: int,
    *,
    artifact_dir: Path | None = None,
) -> dict[int, bool]:
    """Step 11 — Verify cover-page positions for students 2..N.

    Called when ``detect_first_page_cover`` returned True. Checks each
    expected cover position in parallel and returns a dict mapping 0-based
    page index → is_cover bool (page 0 is included as True for completeness).
    The caller decides what to do with mismatches (warn or fail under STRICT).
    """
    from eXercise.ai_client import make_gemini_native_client, parse_model_spec
    from xscore.shared.terminal_ui import ok_line, warn_line, format_duration

    cover_ok: dict[int, bool] = {0: True}

    model, thinking, max_tokens = parse_model_spec(
        os.environ.get("COVER_PAGE_DETECTION_MODEL", "gemini-2.5-flash")
    )
    if model.startswith("gemini"):
        gai_client = make_gemini_native_client()
        if gai_client is None:
            warn_line("GEMINI_API_KEY not set — cover-page verification skipped")
            return cover_ok
    else:
        gai_client = None

    cover_indices = [b * pages_per_student for b in range(1, num_students)]
    if not cover_indices:
        return cover_ok

    def _check(idx: int) -> tuple[int, bool]:
        save_path = (
            artifact_cover_verify_prompt_path(artifact_dir, f"cover_p{idx + 1}")
            if artifact_dir else None
        )
        return idx, is_cover_page(
            cleaned_pdf, idx, gai_client, model,
            prompt_save_path=save_path,
            thinking_tokens=thinking, max_tokens=max_tokens,
        )

    workers = min(
        len(cover_indices),
        int(os.environ.get("COVER_PAGE_WORKERS", "500")),
    )
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for idx, ok in ex.map(_check, cover_indices):
            cover_ok[idx] = ok
    ok_line(
        f"Verified {len(cover_indices)} cover position(s)"
        f"  ·  {format_duration(time.perf_counter() - t0)}"
    )
    return cover_ok


def assign_pages(
    cleaned_pdf: Path,
    students: list[str],
    dpi: int = NAME_RECOGNITION_DPI,
    pages_per_student: int = 1,
    name_crop_fraction: float | None = None,
    *,
    pages: list | None = None,
    artifact_dir: Path | None = None,
    cover_page_mode: bool = False,
) -> list[PageAssignment]:
    """Return one ``PageAssignment`` per student block (Step 12).

    Pages are grouped into fixed blocks of *pages_per_student* (as determined
    by exam geometry in step 10). The name is read from the first page of
    each block. Blocks with no detectable name are recorded as ``Unknown_N``
    with ``confidence="low"`` instead of being merged into a neighbouring
    student.

    Cover-page detection is performed earlier (step 9 ``detect_first_page_cover``
    and step 11 ``verify_cover_positions``); ``cover_page_mode`` is final by
    the time this runs.

    *name_crop_fraction*: fraction of page height to crop for name detection.
    ``None`` (default) auto-detects A4 (top half, 0.5) vs A3 (top third, 1/3)
    from pixel dimensions.  Pass a float to override for all pages.

    *pages*: optional pre-rendered PIL images at *dpi* (skips PDF rendering).
    """
    from xscore.extraction.ground_truth import fuzzy_match_name
    from eXercise.ai_client import make_ai_client

    ai_result = make_ai_client(model_env="NAME_DETECTION_MODEL", default_model="gemini-2.5-flash")
    if ai_result is None:
        raise RuntimeError(
            "NAME_DETECTION_MODEL client could not be created — check API key in .env"
        )
    client, model_id, _provider, _thinking, _max_tok = ai_result

    from xscore.shared.terminal_ui import info_line, ok_line, format_duration, warn_line

    if pages is None:
        import fitz as _fitz
        with _fitz.open(str(cleaned_pdf)) as _d:
            n_pages = _d.page_count
    else:
        n_pages = len(pages)
    import math
    n_blocks = math.ceil(n_pages / pages_per_student)
    first_page_set = {b * pages_per_student + 1 for b in range(n_blocks)}  # 1-based scan pages

    # ------------------------------------------------------------------
    # Name detection — only the first page of each student block is checked.
    # In cover-page mode the first page is the cover page, which is where
    # the student writes their name, so the same positions apply.
    #
    # When *pages* is None we render only the ~n_blocks first pages on demand
    # inside each worker (per-worker fitz doc — fitz is not thread-safe).
    # ------------------------------------------------------------------
    info_line("Detecting student names from scan pages …")
    workers = int(os.environ.get("NAME_WORKERS", str(min(n_blocks, 8))))
    prompt = _make_name_prompt(students) if students else load_prompt("student_names_freeform")[1]

    def _ocr_and_match(idx: int, i: int) -> tuple[int, int, str, str | None, float]:
        """Returns (idx, page, raw_name, matched_name, elapsed_s)."""
        if pages is not None:
            page = pages[i - 1]
        else:
            import fitz as _fitz
            from PIL import Image as _Image
            with _fitz.open(str(cleaned_pdf)) as _d:
                pix = _d[i - 1].get_pixmap(dpi=dpi, colorspace=_fitz.csRGB)
                page = _Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        fraction = name_crop_fraction if name_crop_fraction is not None else _name_crop_fraction(page, dpi)
        crop = _crop_top(page, fraction=fraction)
        img_b64 = page_to_jpeg_b64(crop)
        save_path = artifact_names_prompt_path(artifact_dir, f"name_{i}") if artifact_dir else None
        _t0 = time.perf_counter()
        raw = ai_image_call(
            client, img_b64, prompt, max_tokens=(_max_tok or 64),
            model_id=model_id, provider=_provider, thinking_tokens=_thinking,
            prompt_save_path=save_path, print_latency=False,
        )
        elapsed = time.perf_counter() - _t0
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            data = {}
        raw_name = str(data.get("name", "") or "").strip()
        matched_name = fuzzy_match_name(raw_name, students) if raw_name else None
        return idx, i, raw_name, matched_name, elapsed

    def _emit(i: int, raw_name: str, matched_name: str | None, elapsed: float) -> None:
        # Yellow warn line for any "no match" case (sentinel, empty, or
        # below-threshold fuzzy match); green ok line for a real roster hit.
        log = ok_line if matched_name is not None else warn_line
        log(f"Page {i:3d}/{n_pages}: {raw_name!r}  →  {matched_name!r}  ·  {format_duration(elapsed)}")

    sorted_pages = sorted(first_page_set)
    pending: dict[int, tuple[int, str, str | None, float]] = {}
    next_idx = 0
    page_results: dict[int, str | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_ocr_and_match, idx, i): idx for idx, i in enumerate(sorted_pages)}
        for fut in as_completed(futures):
            idx, i, raw_name, matched_name, elapsed = fut.result()
            pending[idx] = (i, raw_name, matched_name, elapsed)
            page_results[i] = matched_name
            # Drain consecutive ready entries so output stays in scan-page
            # order while remaining incremental as workers complete.
            while next_idx in pending:
                pi, prn, pmn, pel = pending.pop(next_idx)
                _emit(pi, prn, pmn, pel)
                next_idx += 1

    # Restore page order for the block-grouping step below.
    # Non-first pages were not submitted and default to None.
    matched: list[str | None] = [page_results.get(i) for i in range(1, n_pages + 1)]
    if pages is not None:
        del pages  # only meaningful when caller pre-rendered the full list

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

    # If the same roster name was matched on multiple covers, suffix all
    # occurrences in scan-page order so neither booklet overwrites the other
    # downstream. Confidence drops to "low" — at most one of the colliding
    # booklets is the real student; the others are mislabelled. ``Unknown_N``
    # names cannot collide by construction (each block gets a unique b+1).
    from collections import Counter
    name_counts = Counter(a.student_name for a in result)
    duplicates = {n for n, c in name_counts.items() if c > 1}
    if duplicates:
        for dup in sorted(duplicates):
            suffixes = ", ".join(f"{dup}_{i + 1}" for i in range(name_counts[dup]))
            warn_line(
                f"Name {dup!r} matched {name_counts[dup]} cover pages — "
                f"labelling them {suffixes}"
            )
        seen: dict[str, int] = {}
        for a in result:
            if a.student_name in duplicates:
                seen[a.student_name] = seen.get(a.student_name, 0) + 1
                a.student_name = f"{a.student_name}_{seen[a.student_name]}"
                a.confidence = "low"

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


def page_assignments_to_overview(assignments: list[PageAssignment]) -> str:
    """Plain-text overview: one aligned line per student in scan-page order.

    Used for the on-disk artifact (``page_range_overview.txt``). The terminal
    rendering uses :func:`print_page_range_table` instead.

    Pages are guaranteed contiguous by step-12 block grouping. If somehow
    they aren't, fall back to listing all pages so the file is still useful.
    """
    if not assignments:
        return ""
    sorted_a = sorted(assignments, key=lambda x: x.page_numbers[0])
    name_w = max(len(a.student_name) for a in sorted_a)
    lo_w = max(len(str(a.page_numbers[0])) for a in sorted_a)
    hi_w = max(len(str(a.page_numbers[-1])) for a in sorted_a)
    lines = []
    for a in sorted_a:
        rng = _format_page_range(a, lo_w, hi_w)
        lines.append(f"{a.student_name:<{name_w}}  pages {rng}")
    return "\n".join(lines) + "\n"


def _format_page_range(a: PageAssignment, lo_w: int, hi_w: int) -> str:
    """Pre-formatted page-range string with dashes aligned across rows."""
    nums = a.page_numbers
    lo, hi = nums[0], nums[-1]
    contiguous = list(range(lo, hi + 1)) == nums
    if not contiguous:
        return ", ".join(str(p) for p in nums)
    if lo == hi:
        return f"{lo:>{lo_w}}"
    return f"{lo:>{lo_w}}–{hi:>{hi_w}}"


def print_page_range_table(assignments: list[PageAssignment]) -> None:
    """Render a Rich table of student → page range to the terminal.

    Two columns — *Student* (left) and *Pages* (right). Low-confidence rows
    (``Unknown_N`` and duplicate-suffixed students) are styled yellow so the
    user can spot them at a glance.
    """
    if not assignments:
        return
    from rich import box
    from rich.padding import Padding
    from rich.table import Table

    from xscore.shared.terminal_ui import get_console

    sorted_a = sorted(assignments, key=lambda x: x.page_numbers[0])
    lo_w = max(len(str(a.page_numbers[0])) for a in sorted_a)
    hi_w = max(len(str(a.page_numbers[-1])) for a in sorted_a)

    table = Table(
        box=box.HORIZONTALS,
        header_style="dim",
        show_edge=False,
        pad_edge=False,
    )
    table.add_column("Student", justify="left")
    table.add_column("Pages", justify="right")

    for a in sorted_a:
        rng = _format_page_range(a, lo_w, hi_w)
        style = "yellow" if a.confidence == "low" else None
        table.add_row(a.student_name, rng, style=style)

    # Title goes ABOVE the table (printed separately) so it doesn't get
    # truncated to the table's content width when the class is small.
    # ``expand=False`` keeps the table itself from padding to console width.
    console = get_console()
    console.print()
    console.print("    [dim]Page range per student (scan-page order)[/]")
    console.print(Padding(table, (0, 0, 0, 4), expand=False))
    console.print()
