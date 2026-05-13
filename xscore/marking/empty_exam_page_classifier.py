"""Step 14: vision-classify each empty-exam page.

Public entry point: :func:`classify_empty_exam_pages`.

For each page in the empty exam PDF, asks the vision LLM to pick a page type
(cover / instruction / question / blank / writing-space) and read its printed
page number. The Gemini path sends each page as a single-page native PDF
slice; non-Gemini models fall back to rasterized JPEG. Per-page artifacts (the
slice PDF or JPEG, plus prompt-logger sidecars) land in
``14_empty_exam_classification/empty_exam_pages/``.

Refactored out of ``blank_page_detection`` so step 14 owns its own module.
``BlankCheckStatus`` and the vision-client helpers live in
``_blank_page_vision_client``.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from eXercise.api_retry import retry_api_call
from xscore.marking._blank_page_vision_client import (
    BlankCheckStatus,
    _ClientState,
    _build_client_state,
    _coerce_conf,
    _extract_page_as_pdf_bytes,
    _render_page_jpeg,
)


PAGE_TYPE_VOCABULARY: tuple[str, ...] = (
    "cover page",
    "instruction page",
    "question page",
    "blank page",
    "writing space page",
)


# ─────────── Response parser ────────────────────────────────────────────────

def _parse_empty_exam_class(
    raw: str,
) -> tuple[str | None, int | None, int | None, int | None, str]:
    """Parse the step-14 empty-exam page-classification response.

    Returns ``(page_type, page_number, conf_page_type, conf_page_number, problem)``.
    Fields parse independently — a malformed one does not poison the others.
    ``page_type`` is returned verbatim when it's a string; the caller validates
    it against the closed vocabulary. ``page_number`` accepts ``int`` (0..999)
    or ``null``; numeric strings are coerced. ``problem`` is always a string.
    """
    empty: tuple[str | None, int | None, int | None, int | None, str] = (
        None, None, None, None, ""
    )
    if not raw:
        return empty
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty

    pt_raw = data.get("page_type")
    page_type: str | None = pt_raw.strip() if isinstance(pt_raw, str) and pt_raw.strip() else None

    pn_raw = data.get("page_number")
    page_number: int | None = None
    if isinstance(pn_raw, bool):
        page_number = None
    elif isinstance(pn_raw, int):
        page_number = pn_raw
    elif isinstance(pn_raw, str):
        s = pn_raw.strip()
        if s and s.lower() not in ("null", "none"):
            try:
                page_number = int(s)
            except ValueError:
                page_number = None
    if page_number is not None and (page_number <= 0 or page_number > 999):
        page_number = None

    conf_pt = _coerce_conf(data.get("confidence_page_type"))
    conf_pn = _coerce_conf(data.get("confidence_page_number"))

    problem_raw = data.get("problem")
    problem = str(problem_raw).strip() if isinstance(problem_raw, str) else ""
    return page_type, page_number, conf_pt, conf_pn, problem


# ─────────── Structured-output schema + low-level call ──────────────────────


class _EmptyExamPageClassResp(BaseModel):
    """Structured-output schema for the step-14 empty-exam classifier (Gemini path).

    Mirrors ``empty_exam_page_classification.md`` v1's return shape.
    """
    page_type: str | None = None
    page_number: int | None = None
    confidence_page_type: int = 5
    confidence_page_number: int = 5
    problem: str = ""


def _call_empty_exam_class(
    state: _ClientState,
    prompt_text: str,
    model_id: str,
    pdf_bytes: bytes | None,
    jpeg_bytes: bytes | None,
    *,
    max_tokens: int,
    request_timeout: "httpx.Timeout | None" = None,
) -> tuple[str, str]:
    """Single-page vision call for step 14. Sends native PDF on Gemini, JPEG elsewhere."""
    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import build_gemini_thinking_config, split_gemini_response

        assert pdf_bytes is not None
        resp = state.gai.models.generate_content(
            model=model_id,
            contents=[
                gai_types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"),
                gai_types.Part.from_text(text=prompt_text),
            ],
            config=gai_types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
                response_schema=_EmptyExamPageClassResp,
                thinking_config=build_gemini_thinking_config(0),
            ),
        )
        return split_gemini_response(resp)

    # Non-Gemini fallback: rasterized JPEG via OpenAI-compat image_url.
    import base64 as _base64

    from eXercise.ai_client import build_completion_kwargs

    assert jpeg_bytes is not None
    _use_stream, kw = build_completion_kwargs(state.provider, 0, max_tokens)
    _timeout_kw: dict = {"timeout": request_timeout} if request_timeout is not None else {}
    b64 = _base64.b64encode(jpeg_bytes).decode()
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": prompt_text},
    ]}]
    try:
        resp = state.oa.chat.completions.create(
            model=model_id, messages=msgs,
            response_format={"type": "json_object"}, **kw, **_timeout_kw,
        )
    except Exception:  # noqa: BLE001
        resp = state.oa.chat.completions.create(model=model_id, messages=msgs, **kw, **_timeout_kw)
    raw = resp.choices[0].message.content or ""
    thinking_text = getattr(resp.choices[0].message, "reasoning_content", "") or ""
    return raw, thinking_text


def _classify_empty_page(
    state: _ClientState,
    model_id: str,
    pdf_bytes: bytes | None,
    jpeg_bytes: bytes | None,
    save_path: Path | None,
    *,
    max_tokens: int,
    request_timeout: "httpx.Timeout | None" = None,
) -> tuple[str | None, int | None, int | None, int | None, str]:
    """Step-14 per-page call. Returns (page_type, page_number, conf_pt, conf_pn, problem).

    Each field is ``None`` (or ``""`` for ``problem``) if the call failed or the
    field was missing/malformed; fields parse independently.
    """
    from xscore.shared.prompt_logger import (
        attachment_part, save_output_data, save_prompt, save_response,
    )
    from xscore.prompts.loader import load_prompt

    _, prompt_text = load_prompt("empty_exam_page_classification")
    prompt_text = prompt_text.rstrip("\n")
    if pdf_bytes is not None:
        attachment = attachment_part(pdf_bytes, "application/pdf")
    else:
        assert jpeg_bytes is not None
        attachment = attachment_part(jpeg_bytes, "image/jpeg")
    save_prompt(
        save_path, model=model_id,
        messages=[{"role": "user", "content": [
            attachment,
            {"type": "text", "text": prompt_text},
        ]}],
    )
    try:
        raw, thinking_text = retry_api_call(
            lambda: _call_empty_exam_class(
                state, prompt_text, model_id, pdf_bytes, jpeg_bytes,
                max_tokens=max_tokens,
                request_timeout=request_timeout,
            ),
            label="Empty-exam page classification",
        )
    except Exception:
        return None, None, None, None, ""
    save_response(save_path, raw, thinking=thinking_text)
    save_output_data(save_path, raw, ext="json")
    return _parse_empty_exam_class(raw)


# ─────────── Public entry point ──────────────────────────────────────────────


def classify_empty_exam_pages(
    empty_exam_pdf: Path,
    artifact_dir: Path | None = None,
    *,
    model_id: str,
    thinking_tokens: int | None = None,
    max_tokens: int | None = None,
) -> tuple[BlankCheckStatus, str | None, list[dict]]:
    """Step 14: vision-classify each empty-exam page in parallel.

    For each page in *empty_exam_pdf*, asks the vision LLM to pick a page type
    (cover/instruction/question/blank/writing-space) and read its printed page
    number. The Gemini path sends each page as a single-page native PDF slice;
    non-Gemini models fall back to rasterized JPEG.

    Returns ``(status, message, classifications)``. The classifications list is
    one dict per page (1-based ``page`` field). Per-page artifacts (the slice
    PDF or JPEG, plus prompt-logger sidecars) are written to
    ``14_empty_exam_classification/empty_exam_pages/``.
    """
    from xscore.shared.exam_paths import artifact_empty_exam_pages_dir
    from xscore.shared.terminal_ui import format_duration, ok_line, warn_line

    client_or_err = _build_client_state(model_id)
    if isinstance(client_or_err, str):
        return BlankCheckStatus.INCONCLUSIVE, client_or_err, []
    state = client_or_err

    import fitz
    with fitz.open(str(empty_exam_pdf)) as _doc:
        n_pages = _doc.page_count
    if n_pages <= 0:
        return BlankCheckStatus.INCONCLUSIVE, "empty exam PDF has zero pages", []
    page_width = max(1, len(str(n_pages)))

    if artifact_dir is not None:
        out_dir = artifact_empty_exam_pages_dir(artifact_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
    else:
        out_dir = None

    use_pdf = model_id.startswith("gemini")
    max_tok = max_tokens or 256

    from eXercise.ai_client import make_request_timeout  # noqa: PLC0415
    request_timeout = make_request_timeout("standard")

    def _do_one(
        idx: int, page: int
    ) -> tuple[int, int, str | None, int | None, int | None, int | None, str, str]:
        if use_pdf:
            pdf_bytes = _extract_page_as_pdf_bytes(empty_exam_pdf, page)
            jpeg_bytes = None
            if out_dir is not None:
                (out_dir / f"page_{page:03d}.pdf").write_bytes(pdf_bytes)
        else:
            jpeg_bytes = _render_page_jpeg(empty_exam_pdf, page)
            pdf_bytes = None
            if out_dir is not None:
                (out_dir / f"page_{page:03d}.jpg").write_bytes(jpeg_bytes)
        save_path = (
            out_dir / f"empty_page_{page:03d}_prompt.txt"
            if out_dir is not None else None
        )
        t0 = time.perf_counter()
        page_type, page_number, conf_pt, conf_pn, problem = _classify_empty_page(
            state, model_id, pdf_bytes, jpeg_bytes, save_path, max_tokens=max_tok,
            request_timeout=request_timeout,
        )
        dur = format_duration(time.perf_counter() - t0)
        return idx, page, page_type, page_number, conf_pt, conf_pn, problem, dur

    workers = min(
        n_pages,
        int(os.environ.get("EMPTY_EXAM_PAGE_CLASSIFICATION_WORKERS", "16")),
    )
    pending: dict[int, tuple[int, str | None, int | None, int | None, int | None, str, str]] = {}
    classifications: list[dict] = [None] * n_pages  # type: ignore[list-item]
    next_idx = 0
    inconclusive_count = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_do_one, i, p): i for i, p in enumerate(range(1, n_pages + 1))}
        for fut in as_completed(futs):
            idx, page, page_type, page_number, conf_pt, conf_pn, problem, dur = fut.result()
            pending[idx] = (page, page_type, page_number, conf_pt, conf_pn, problem, dur)
            while next_idx in pending:
                pg, pt, pn, c_pt, c_pn, prob, d = pending.pop(next_idx)
                # Validate page_type against the closed vocabulary.
                if pt is not None and pt not in PAGE_TYPE_VOCABULARY:
                    extra = (
                        f"; page_type {pt!r} not in vocabulary"
                    )
                    prob = (prob + extra) if prob else extra.lstrip("; ")
                    pt = None
                # Cover pages with page_number == None are normal — don't flag.
                if (pt is None or pn is None) and pt != "cover page":
                    inconclusive_count += 1
                # Display
                pt_label = pt if pt is not None else "?"
                pn_label = str(pn) if pn is not None else "—"
                conf_min: int | None
                _confs = [c for c in (c_pt, c_pn) if c is not None]
                conf_min = min(_confs) if _confs else None
                line_fn = warn_line if prob else ok_line
                conf_str = f"conf={conf_min}" if conf_min is not None else "conf=?"
                problem_suffix = ""
                if prob:
                    p_short = prob if len(prob) <= 120 else prob[:119].rstrip() + "…"
                    problem_suffix = f"  ·  {p_short}"
                line_fn(
                    f"Page {pg:>{page_width}d}/{n_pages}"
                    f"  ·  {pt_label:<18}  ·  pg {pn_label:<5}"
                    f"  ·  {conf_str:<7}  ·  {d}{problem_suffix}"
                )
                classifications[next_idx] = {
                    "page": pg,
                    "page_type": pt,
                    "page_number": pn,
                    "confidence_page_type": c_pt,
                    "confidence_page_number": c_pn,
                    "problem": prob,
                }
                next_idx += 1

    if inconclusive_count == n_pages and inconclusive_count > 0:
        return (
            BlankCheckStatus.INCONCLUSIVE,
            "all empty-exam pages failed classification (model errors or malformed responses)",
            classifications,
        )
    if inconclusive_count > 0:
        return (
            BlankCheckStatus.INCONCLUSIVE,
            f"{inconclusive_count}/{n_pages} empty-exam page(s) had missing fields or low confidence",
            classifications,
        )
    return BlankCheckStatus.PASSED, f"{n_pages} pages classified", classifications
