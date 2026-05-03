"""Cover-page detection for empty-exam PDFs and student scans.

Two pipeline-level entry points:

- :func:`detect_empty_exam_cover` — informational check on page 1 of the empty
  exam PDF (sets ``ctx.cover_page_empty`` for downstream geometry).
- :func:`detect_first_page_cover` — checks scan page 1 for a cover page (sets
  ``ctx.cover_page_mode`` for downstream geometry).

Each entry point selects between two underlying probes:

- :func:`is_cover_page` — OCR + text-only AI call (used for raster scans).
- :func:`check_cover_page_text` — fitz text extraction + text-only AI call
  (used for vector empty-exam PDFs).

Cover-detection model: ``COVER_PAGE_DETECTION_MODEL`` env var (scans);
``EMPTY_EXAM_COVER_MODEL`` env var (empty exam). Both default to
``gemini-2.5-flash``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from xscore.config import COVER_PAGE_DETECTION_DPI, GEMINI_MAX_OUTPUT_TOKENS

from eXercise.api_retry import retry_api_call
from xscore.prompts.loader import load_prompt
from xscore.shared.exam_paths import (
    artifact_cover_page_dir,
    artifact_cover_scan_prompt_path,
)
from xscore.shared.prompt_logger import save_prompt, save_response


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

    _, system_prompt = load_prompt("cover_page_scan", section="system")
    _, user_prompt = load_prompt(
        "cover_page_scan", section="user", text=printed_text or "(no text extracted)",
    )

    save_prompt(prompt_save_path, model=model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ])

    thinking_text = ""
    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import build_gemini_thinking_config
        config = gai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=max_tokens or GEMINI_MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
            # Audit item [28]: schema is a plain string so the model can return
            # the enum token; the parser maps cover/instructions → True. The
            # bool fallback in _parse_cover_bool still handles legacy responses.
            response_schema=str,
            thinking_config=build_gemini_thinking_config(thinking_tokens),
        )
        resp = gai_client.models.generate_content(
            model=model_id,
            contents=[gai_types.Part.from_text(text=user_prompt)],
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
        _msgs = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt + '\n\nReturn JSON only with this shape: {"page_type": "cover" | "instructions" | "question"}'},
        ]
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
    parsed = _parse_cover_bool(raw)
    try:
        from xscore.shared.prompt_logger import save_output_data
        save_output_data(
            prompt_save_path, json.dumps({"is_cover_page": parsed}, indent=2),
            ext="json",
        )
    except Exception:  # noqa: BLE001
        pass
    return parsed


def _parse_cover_bool(raw: str) -> bool:
    """Parse the model response for cover-page detection.

    Audit item [28]/[30] introduced a three-way enum `page_type` (cover /
    instructions / question). For the existing boolean call site, both
    `cover` and `instructions` map to True (neither has questions, so the
    downstream "skip non-question pages" logic still works during the
    alias-then-flip migration).

    Also tolerates the legacy shapes:
    - Gemini's bare ``true``/``false`` (from ``response_schema=bool``).
    - OpenAI-compat ``{"answer": <bool>}``.
    - Older prompt versions emitting ``is_cover`` / ``is_cover_page`` directly.
    """
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return False
    if isinstance(data, bool):
        return data
    if isinstance(data, str):
        # Gemini path with response_schema=str returns a bare string ("cover" etc.).
        return data.strip().lower() in ("cover", "instructions")
    if isinstance(data, dict):
        # New prompt shape (cover_page_scan v5+): {"page_type": "cover" | "instructions" | "question"}.
        page_type = data.get("page_type")
        if isinstance(page_type, str):
            return page_type.strip().lower() in ("cover", "instructions")
        # Legacy shapes — accept any of the historical key names.
        return bool(data.get("is_cover", data.get("answer", data.get("is_cover_page", False))))
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

    _, system_prompt = load_prompt("cover_page_scan", section="system")
    _, user_prompt = load_prompt(
        "cover_page_scan", section="user", text=page_text or "(no text extracted)",
    )

    save_prompt(prompt_save_path, model=model_id,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ])

    thinking_text = ""
    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import build_gemini_thinking_config
        config = gai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=max_tokens or GEMINI_MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
            # Audit item [28]: schema is a plain string so the model can return
            # the enum token; the parser maps cover/instructions → True. The
            # bool fallback in _parse_cover_bool still handles legacy responses.
            response_schema=str,
            thinking_config=build_gemini_thinking_config(thinking_tokens),
        )
        resp = retry_api_call(
            lambda: gai_client.models.generate_content(
                model=model_id,
                contents=[gai_types.Part.from_text(text=user_prompt)],
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
        _msgs = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt + '\n\nReturn JSON only with this shape: {"page_type": "cover" | "instructions" | "question"}'},
        ]
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
    parsed = _parse_cover_bool(raw)
    try:
        from xscore.shared.prompt_logger import save_output_data
        save_output_data(
            prompt_save_path, json.dumps({"is_cover_page": parsed}, indent=2),
            ext="json",
        )
    except Exception:  # noqa: BLE001
        pass
    return parsed


def detect_empty_exam_cover(
    exam_pdf: Path,
    *,
    artifact_dir: Path | None = None,
) -> bool | None:
    """Check whether page 1 of the empty exam PDF is a cover page.

    Returns True/False on success, or None when skipped (no API key, missing
    google-genai, etc.). The None case is meaningful: ``compute_geometry``
    raises on (empty=None, scan=True) so we never silently default the cover
    offset when the empty-exam cover check didn't produce a value.
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
        save_path = save_dir / "cover_empty_exam_prompt.txt"

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
    """Check whether scan page 1 is a cover page.

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
