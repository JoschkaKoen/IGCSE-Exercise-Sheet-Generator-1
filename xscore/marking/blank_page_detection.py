"""Steps 14 + 17: check student scans for handwriting, and detect blank pages in the empty exam.

Step 14 (student_handwriting_check): vision LLM call per (student × answer page).
Renders scan pages as JPEGs and checks for student handwriting. Under
``HANDWRITING_CHECK_WIDE=1`` (default) every answer page is checked; under
``=0`` only step-17 blank pages are checked.
Writes ``14_student_handwriting/handwriting.json``.

Step 17 (exam_blank_detection): text-only LLM call. Reads every page's extracted
text from the empty exam PDF and identifies which pages are blank (no question text,
only writing lines or "BLANK PAGE" heading). Writes
``17_exam_blank_detection/blank_exam_pages.json``.

Both functions emit per-page / per-task progress lines via the terminal_ui
``info_line`` / ``ok_line`` / ``warn_line`` helpers (mirrors step 15's
``_ocr_and_match`` idiom). Policy stays at the dispatcher: INCONCLUSIVE
returns from these functions; the dispatcher in ``xscore/steps/geometry.py``
decides warn-vs-SystemExit based on the per-step ``*_STRICT`` env var.

Pages where ``_has_handwriting`` could not be determined are **omitted** from
``blank_scan_pages`` and ``pages_without_handwriting`` (so the consumer in
``xscore/marking/marking_page_register.py`` is unaffected) and listed under a
sibling ``inconclusive_pages`` field per student.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class BlankCheckStatus(Enum):
    PASSED = "PASSED"
    INCONCLUSIVE = "INCONCLUSIVE"


from eXercise.api_retry import retry_api_call


# ─────────── Text + image extraction ─────────────────────────────────────────

def _exam_page_texts(exam_pdf: Path) -> list[str]:
    import fitz
    with fitz.open(str(exam_pdf)) as doc:
        return [doc[i].get_text().strip() for i in range(doc.page_count)]


def _render_page_jpeg(pdf_path: Path, page_1based: int, dpi: int = 150) -> bytes:
    import fitz
    with fitz.open(str(pdf_path)) as doc:
        pix = doc[page_1based - 1].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    return pix.tobytes("jpeg")


# ─────────── Model client (shared by both helpers) ───────────────────────────

class _ClientState:
    def __init__(self, gai: Any, oa: Any, provider: str | None) -> None:
        self.gai = gai
        self.oa = oa
        self.provider = provider


def _build_client_state(model_id: str) -> _ClientState | str:
    """Return ``_ClientState`` on success, or a human-readable error message string."""
    if model_id.startswith("gemini"):
        from eXercise.ai_client import make_gemini_native_client
        gai = make_gemini_native_client()
        if gai is None:
            return "GEMINI_API_KEY not set"
        return _ClientState(gai=gai, oa=None, provider="gemini")
    from eXercise.ai_client import make_ai_client
    result = make_ai_client(model_env="", default_model=model_id)
    if result is None:
        return f"model={model_id} requires API key for its provider"
    oa, _, provider, _, _ = result
    return _ClientState(gai=None, oa=oa, provider=provider)


# ─────────── Response parsers ────────────────────────────────────────────────

def _parse_blank_pages(raw: str) -> set[int] | None:
    """Parse blank-page list. Returns ``set[int]`` on success (possibly empty),
    or ``None`` when the response is malformed / unusable.

    Accepts either Gemini ``[1, 2, 3]`` or OA ``{"blank_pages": [...]}`` shapes.
    An empty result list is *legitimate* (means "no blanks found") and returns
    ``set()``; only structural failures return ``None``.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("blank_pages")
        if pages is None:
            pages = data.get("pages")
        if pages is None:
            return None
    else:
        return None
    if not isinstance(pages, list):
        return None
    try:
        return {int(p) for p in pages}
    except (TypeError, ValueError):
        return None


def _parse_handwriting(
    raw: str,
) -> tuple[bool | None, int | None, bool | None]:
    """Parse the handwriting + page-number + cover-page vision response.

    Returns ``(handwriting, page_number, is_cover_page)``. Each component is
    parsed independently — a malformed field does not invalidate the others.
    Any component may be ``None`` when the field was missing or malformed.
    """
    if not raw:
        return None, None, None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, None, None
    if isinstance(data, bool):
        return data, None, None
    if not isinstance(data, dict):
        return None, None, None
    hw_raw = data.get("answer")
    if hw_raw is None:
        hw_raw = data.get("has_handwriting")
    hw: bool | None = hw_raw if isinstance(hw_raw, bool) else None
    pn_raw = data.get("page_number")
    pn: int | None = None
    if isinstance(pn_raw, bool):
        pn = None  # bool is a subclass of int — reject
    elif isinstance(pn_raw, int):
        pn = pn_raw
    elif isinstance(pn_raw, str):
        try:
            pn = int(pn_raw.strip())
        except (ValueError, AttributeError):
            pn = None
    if pn is not None and (pn <= 0 or pn > 200):
        pn = None
    cover_raw = data.get("is_cover_page")
    cover: bool | None = cover_raw if isinstance(cover_raw, bool) else None
    return hw, pn, cover


# ─────────── Step 17: find blank pages in empty exam ─────────────────────────

def find_blank_exam_pages(
    state: _ClientState,
    exam_texts: list[str],
    model_id: str,
    artifact_dir: Path | None,
    *,
    thinking_tokens: int | None = None,
    max_tokens: int | None = None,
) -> set[int] | None:
    """One LLM text call to identify blank exam pages.

    Returns ``set[int]`` of 1-based page numbers (possibly empty) on success;
    ``None`` when the call could not be completed or the response was malformed.

    The prompt includes the full candidate list so the model cannot hallucinate
    out-of-range numbers. The parsed result is additionally clipped to the valid
    set as a second layer of defence.
    """
    from xscore.shared.prompt_logger import save_prompt, save_response
    from xscore.shared.exam_paths import (
        artifact_blank_detection_txt_path,
        artifact_exam_blank_prompt_path,
    )
    from xscore.prompts.loader import load_prompt

    num_pages = len(exam_texts)
    candidates = list(range(1, num_pages + 1))

    page_lines: list[str] = []
    for i, text in enumerate(exam_texts, 1):
        page_lines += [f"Page {i}:", text or "(no printed text)", ""]
    exam_pages_block = "\n".join(page_lines)

    _, prompt = load_prompt(
        "exam_blank_detection",
        exam_pages_block=exam_pages_block,
        num_pages=num_pages,
        page_word="page" if num_pages == 1 else "pages",
        candidates=candidates,
    )
    prompt = prompt.rstrip("\n")

    if artifact_dir:
        det_path = artifact_blank_detection_txt_path(artifact_dir)
        det_path.parent.mkdir(parents=True, exist_ok=True)
        det_path.write_text(prompt, encoding="utf-8")

    save_path = (
        artifact_exam_blank_prompt_path(artifact_dir, "blank_detection_exam")
        if artifact_dir else None
    )
    save_prompt(save_path, model=model_id, messages=[{"role": "user", "content": prompt}])

    try:
        raw, thinking_text = retry_api_call(
            lambda: _call_blank_detection(state, prompt, model_id, thinking_tokens, max_tokens),
            label="Blank page detection (exam)",
        )
    except Exception:
        return None
    save_response(save_path, raw, thinking=thinking_text)

    result = _parse_blank_pages(raw)
    if result is None:
        return None
    valid = set(range(1, num_pages + 1))
    return result & valid


def _call_blank_detection(
    state: _ClientState,
    prompt: str,
    model_id: str,
    thinking_tokens: int | None,
    max_tokens: int | None,
) -> tuple[str, str]:
    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import build_gemini_thinking_config, split_gemini_response
        cfg_kwargs: dict = {
            "max_output_tokens": max_tokens or 256,
            "response_mime_type": "application/json",
            "response_schema": list[int],
        }
        if thinking_tokens is not None:
            cfg_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
        resp = state.gai.models.generate_content(
            model=model_id,
            contents=[gai_types.Part.from_text(text=prompt)],
            config=gai_types.GenerateContentConfig(**cfg_kwargs),
        )
        return split_gemini_response(resp)
    from eXercise.ai_client import build_completion_kwargs, collect_streamed_response
    use_stream, kw = build_completion_kwargs(state.provider, thinking_tokens, max_tokens or 256)
    oa_prompt = prompt + '\n\nReturn JSON only with this shape: {"blank_pages": [<int>, ...]}'
    msgs = [{"role": "user", "content": oa_prompt}]
    if use_stream:
        _th: list[str] = []
        stream = state.oa.chat.completions.create(model=model_id, messages=msgs, stream=True, **kw)
        return collect_streamed_response(stream, thinking_out=_th), "".join(_th)
    try:
        resp = state.oa.chat.completions.create(
            model=model_id, messages=msgs,
            response_format={"type": "json_object"}, **kw,
        )
    except Exception:  # noqa: BLE001
        resp = state.oa.chat.completions.create(model=model_id, messages=msgs, **kw)
    raw = resp.choices[0].message.content or ""
    thinking_text = getattr(resp.choices[0].message, "reasoning_content", "") or ""
    return raw, thinking_text


# ─────────── Step 14: per-page handwriting check ──────────────────────────────

def _has_handwriting(
    state: _ClientState,
    model_id: str,
    jpeg_bytes: bytes,
    save_path: Path | None,
) -> tuple[bool | None, int | None, bool | None]:
    """Vision call: handwriting + printed page number + cover-page flag.

    Returns ``(handwriting, page_number, is_cover_page)``. Each component is
    ``None`` if the call failed or the field was missing/malformed; the three
    are independent so any subset can succeed.
    """
    from xscore.shared.prompt_logger import attachment_part, save_prompt, save_response
    from xscore.prompts.loader import load_prompt

    _, prompt_text = load_prompt("student_handwriting_check")
    prompt_text = prompt_text.rstrip("\n")
    save_prompt(
        save_path, model=model_id,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt_text},
            attachment_part(jpeg_bytes, "image/jpeg"),
        ]}],
    )

    try:
        raw, thinking_text = retry_api_call(
            lambda: _call_handwriting(state, prompt_text, model_id, jpeg_bytes),
            label="Handwriting check",
        )
    except Exception:
        return None, None, None
    save_response(save_path, raw, thinking=thinking_text)
    return _parse_handwriting(raw)


class _HandwritingPageNumberResp(BaseModel):
    """Structured-output schema for the step-15 vision call (Gemini path)."""
    answer: bool
    page_number: int | None = None
    is_cover_page: bool = False


def _call_handwriting(
    state: _ClientState,
    prompt_text: str,
    model_id: str,
    jpeg_bytes: bytes,
) -> tuple[str, str]:
    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import split_gemini_response
        resp = state.gai.models.generate_content(
            model=model_id,
            contents=[
                gai_types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"),
                gai_types.Part.from_text(text=prompt_text),
            ],
            config=gai_types.GenerateContentConfig(
                max_output_tokens=96,
                response_mime_type="application/json",
                response_schema=_HandwritingPageNumberResp,
            ),
        )
        return split_gemini_response(resp)
    import base64 as _base64
    from eXercise.ai_client import build_completion_kwargs
    # Force thinking off — the JSON shape is in the prompt, 96 tokens is plenty.
    _use_stream, kw = build_completion_kwargs(state.provider, 0, 96)
    b64 = _base64.b64encode(jpeg_bytes).decode()
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": prompt_text},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]}]
    try:
        resp = state.oa.chat.completions.create(
            model=model_id, messages=msgs,
            response_format={"type": "json_object"}, **kw,
        )
    except Exception:  # noqa: BLE001
        resp = state.oa.chat.completions.create(model=model_id, messages=msgs, **kw)
    raw = resp.choices[0].message.content or ""
    thinking_text = getattr(resp.choices[0].message, "reasoning_content", "") or ""
    return raw, thinking_text


# ─────────── Public entry points ──────────────────────────────────────────────

def check_exam_blank_pages(
    exam_pdf: Path,
    artifact_dir: Path | None = None,
) -> tuple[BlankCheckStatus, str | None]:
    """Step 14: detect blank pages in the empty exam PDF (text-only LLM).

    Writes ``14_exam_blank_detection/blank_exam_pages.json`` to artifact_dir.
    Emits ``Checking N empty-exam pages …`` and a ``✓ Page i/N · {blank|content}``
    line per page. Returns ``(BlankCheckStatus, message)``; never raises
    SystemExit (the dispatcher owns warn-vs-SystemExit policy).
    """
    from eXercise.ai_client import parse_model_spec
    from xscore.shared.exam_paths import artifact_exam_blank_json_path
    from xscore.shared.terminal_ui import info_line, ok_line

    model_id, thinking, max_tok = parse_model_spec(
        os.environ.get("EXAM_BLANK_DETECTION_MODEL", "qwen3.6-flash")
    )

    client_or_err = _build_client_state(model_id)
    if isinstance(client_or_err, str):
        return BlankCheckStatus.INCONCLUSIVE, client_or_err
    state = client_or_err

    exam_texts = _exam_page_texts(exam_pdf)
    n_pages = len(exam_texts)
    info_line(f"Checking {n_pages} empty-exam pages for blanks …")
    blank_exam_pages = find_blank_exam_pages(
        state, exam_texts, model_id, artifact_dir,
        thinking_tokens=thinking, max_tokens=max_tok,
    )
    if blank_exam_pages is None:
        return (
            BlankCheckStatus.INCONCLUSIVE,
            "could not determine which exam pages are blank "
            "(model call failed or returned malformed response)",
        )

    width = len(str(n_pages))
    for i in range(1, n_pages + 1):
        label = "blank" if i in blank_exam_pages else "content"
        ok_line(f"Page {i:>{width}d}/{n_pages}  ·  {label}")

    result_doc = {"blank_exam_pages": sorted(blank_exam_pages)}
    if artifact_dir:
        bp_path = artifact_exam_blank_json_path(artifact_dir)
        bp_path.parent.mkdir(parents=True, exist_ok=True)
        bp_path.write_text(json.dumps(result_doc, indent=2), encoding="utf-8")

    if not blank_exam_pages:
        return BlankCheckStatus.PASSED, "no blank pages found in empty exam"
    n = len(blank_exam_pages)
    pages_label = (
        f"exam page{'s' if n != 1 else ''} {sorted(blank_exam_pages)} "
        f"{'are' if n != 1 else 'is'} blank"
    )
    return BlankCheckStatus.PASSED, pages_label


def check_student_handwriting(
    scan_pdf: Path,
    artifact_dir: Path | None = None,
    *,
    cover_page_mode: bool = False,
    pages_per_student: int = 0,
    cover_offset: int = 0,
) -> tuple[BlankCheckStatus, str | None]:
    """Step 15: per-scan-page vision classification.

    For every scan PDF page, runs a vision LLM call that returns
    ``(has_handwriting, detected_page_number, is_cover_page)``. Per-student
    aggregation happens later in the marking-page-register builder, which has
    the page assignments needed to map scan pages → students.

    Writes ``15_student_handwriting/handwriting.json`` with a flat
    ``scan_pages`` list (one entry per page) and a ``metadata`` block
    capturing the geometry parameters used to compute expected values.

    Returns ``(BlankCheckStatus, message)``; never raises SystemExit (the
    dispatcher owns warn-vs-SystemExit policy).
    """
    from eXercise.ai_client import parse_model_spec
    from xscore.shared.exam_paths import (
        artifact_handwriting_dir,
        artifact_handwriting_json_path,
        artifact_handwriting_prompt_path,
    )
    from xscore.shared.terminal_ui import (
        format_duration,
        info_line,
        ok_line,
        warn_line,
    )

    if artifact_dir is None:
        return BlankCheckStatus.INCONCLUSIVE, "no artifact_dir provided"
    if pages_per_student <= 0:
        return (
            BlankCheckStatus.INCONCLUSIVE,
            f"invalid pages_per_student={pages_per_student}; geometry must run first",
        )

    model_id, _thinking, _max_tok = parse_model_spec(
        os.environ.get("HANDWRITING_CHECK_MODEL", "qwen3-vl-flash")
    )
    client_or_err = _build_client_state(model_id)
    if isinstance(client_or_err, str):
        return BlankCheckStatus.INCONCLUSIVE, client_or_err
    state = client_or_err

    import fitz
    with fitz.open(str(scan_pdf)) as _doc:
        scan_n_pages = _doc.page_count
    page_width = max(1, len(str(scan_n_pages)))

    def _expected(scan_page: int) -> int:
        """Return expected exam_page for this scan_page, or 0 for an expected cover."""
        p_label = ((scan_page - 1) % pages_per_student) + 1
        if cover_page_mode and p_label == 1:
            return 0
        return p_label - cover_offset

    # Build task list. exam_page = 0 → cover; exam_page >= 1 → answer page.
    # Skip rows where exam_page < 1 (can occur when cover_offset is -1 and the
    # first scan page is a non-cover that maps to "exam page 0", i.e. before
    # the empty exam's first content page).
    tasks: list[tuple[int, int]] = []  # (scan_page, exam_page)
    for scan_page in range(1, scan_n_pages + 1):
        exam_page = _expected(scan_page)
        if exam_page == 0 or exam_page >= 1:
            tasks.append((scan_page, exam_page))

    if not tasks:
        artifact = {
            "metadata": {
                "scan_pdf_page_count": scan_n_pages,
                "cover_page_mode": cover_page_mode,
                "pages_per_student": pages_per_student,
                "cover_offset": cover_offset,
            },
            "scan_pages": [],
            "inconclusive_pages": [],
        }
        hw_path = artifact_handwriting_json_path(artifact_dir)
        hw_path.parent.mkdir(parents=True, exist_ok=True)
        hw_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
        return BlankCheckStatus.PASSED, "no scan pages to check"

    jpeg_dir = artifact_handwriting_dir(artifact_dir)
    jpeg_dir.mkdir(parents=True, exist_ok=True)

    info_line(
        f"Checking {len(tasks)} scan pages for handwriting + page-number + cover-page …"
    )

    def _classify(
        exam_page: int, detected_pn: int | None, is_cover: bool | None
    ) -> tuple[str, bool | None]:
        """Return (page-number column text, match flag).

        match: True when the AI's classification matches expectation (cover ↔
        cover, or answer page with correct printed page number); False on
        clear disagreement; None when not enough info to decide.
        """
        expected_is_cover = (exam_page == 0)
        if is_cover:
            ai_label = "cover"
        elif detected_pn is not None:
            ai_label = str(detected_pn)
        else:
            ai_label = "—"
        if expected_is_cover:
            match: bool | None = None if is_cover is None else bool(is_cover)
            expected_label = "cover"
        else:
            if is_cover:
                match = False
            elif detected_pn is None:
                match = None
            else:
                match = (detected_pn == exam_page)
            expected_label = str(exam_page)
        if match is False:
            return f"pg {ai_label} (exp {expected_label})", False
        return f"pg {ai_label}", match

    def _detect(
        idx: int, args: tuple
    ) -> tuple[int, int, int, bool | None, int | None, bool | None, str]:
        """Returns (idx, scan_page, exam_page, hw, detected_pn, is_cover, dur_str)."""
        scan_page, exam_page = args
        jpeg_bytes = _render_page_jpeg(scan_pdf, scan_page)
        (jpeg_dir / f"page_{scan_page:03d}.jpg").write_bytes(jpeg_bytes)
        save_path = artifact_handwriting_prompt_path(
            artifact_dir, f"page_{scan_page:03d}"
        )
        t0 = time.perf_counter()
        hw, detected_pn, is_cover = _has_handwriting(state, model_id, jpeg_bytes, save_path)
        dur = format_duration(time.perf_counter() - t0)
        return idx, scan_page, exam_page, hw, detected_pn, is_cover, dur

    def _emit(
        scan_page: int,
        exam_page: int,
        hw: bool | None,
        detected_pn: int | None,
        is_cover: bool | None,
        dur: str,
    ) -> None:
        pn_str, match = _classify(exam_page, detected_pn, is_cover)
        if hw is None:
            label = "inconclusive   "
            line_fn = warn_line
        else:
            label = "has handwriting" if hw else "no handwriting "
            line_fn = warn_line if match is False else ok_line
        line_fn(
            f"Page {scan_page:>{page_width}d}/{scan_n_pages}  ·  {label}  ·  {pn_str:<20}  ·  {dur}"
        )

    results: list[tuple[int, int, bool | None, int | None, bool | None]] = []
    pending: dict[int, tuple[int, int, bool | None, int | None, bool | None, str]] = {}
    next_idx = 0
    workers = min(len(tasks), int(os.environ.get("HANDWRITING_WORKERS", "32")))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_detect, i, t): i for i, t in enumerate(tasks)}
        for fut in as_completed(futs):
            idx, scan_page, exam_page, hw, detected_pn, is_cover, dur = fut.result()
            pending[idx] = (scan_page, exam_page, hw, detected_pn, is_cover, dur)
            while next_idx in pending:
                sp, ep, h, pn, ic, d = pending.pop(next_idx)
                _emit(sp, ep, h, pn, ic, d)
                results.append((sp, ep, h, pn, ic))
                next_idx += 1

    # ── Build artifact ───────────────────────────────────────────────────────
    scan_pages_out: list[dict] = []
    inconclusive_pages: list[dict] = []
    mismatch_total: list[tuple[int, str, str]] = []
    for scan_page, exam_page, has_hw, detected_pn, is_cover in results:
        expected_is_cover = (exam_page == 0)
        expected_pn: int | None = None if expected_is_cover else exam_page
        _, pn_match = _classify(exam_page, detected_pn, is_cover)
        scan_pages_out.append({
            "scan_page": scan_page,
            "expected_is_cover": expected_is_cover,
            "expected_page_number": expected_pn,
            "has_handwriting": has_hw,
            "detected_page_number": detected_pn,
            "is_cover_page": is_cover,
            "match": pn_match,
        })
        if has_hw is None:
            inconclusive_pages.append({
                "scan_page": scan_page,
                "reason": "handwriting check failed (model error or malformed response)",
            })
        if pn_match is False:
            expected_label = "cover" if expected_is_cover else str(exam_page)
            if is_cover:
                detected_label = "cover"
            elif detected_pn is not None:
                detected_label = str(detected_pn)
            else:
                detected_label = "—"
            mismatch_total.append((scan_page, expected_label, detected_label))

    artifact = {
        "metadata": {
            "scan_pdf_page_count": scan_n_pages,
            "cover_page_mode": cover_page_mode,
            "pages_per_student": pages_per_student,
            "cover_offset": cover_offset,
        },
        "scan_pages": scan_pages_out,
        "inconclusive_pages": inconclusive_pages,
    }
    hw_path = artifact_handwriting_json_path(artifact_dir)
    hw_path.parent.mkdir(parents=True, exist_ok=True)
    hw_path.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Tallies — cover pages excluded from handwriting tallies (their
    # has_handwriting result is not consumed downstream).
    answer_results = [r for r in results if r[1] != 0]
    hw_count = sum(1 for r in answer_results if r[2] is True)
    n_done = sum(1 for r in answer_results if r[2] is not None)
    n_no_pn = sum(1 for r in answer_results if r[2] is not None and r[3] is None)
    n_total = len(results)

    if mismatch_total:
        items = ", ".join(
            f"page {sp}: detected {det}, expected {exp}"
            for sp, exp, det in mismatch_total[:10]
        )
        more = (
            f" (and {len(mismatch_total) - 10} more)"
            if len(mismatch_total) > 10 else ""
        )
        warn_line(
            f"Page-classification mismatches ({len(mismatch_total)}): {items}{more} — "
            "advisory only; check whether the scan is misordered."
        )
    if n_done > 0 and n_no_pn / n_done > 0.20:
        info_line(
            f"Page-number not detected on {n_no_pn}/{n_done} answer pages "
            "(model returned null or unreadable)."
        )

    hw_label = "no handwriting" if hw_count == 0 else f"{hw_count}/{n_done} with handwriting"

    if inconclusive_pages:
        sample = ", ".join(f"page {p['scan_page']}" for p in inconclusive_pages[:10])
        more = (
            f" (and {len(inconclusive_pages) - 10} more)"
            if len(inconclusive_pages) > 10 else ""
        )
        msg = (
            f"Verified {n_done}/{n_total} answer-page handwriting checks; "
            f"could not verify: {sample}{more} — these scan pages will not be "
            "attached to any answer; review manually if continuation work is suspected."
        )
        return BlankCheckStatus.INCONCLUSIVE, msg

    return BlankCheckStatus.PASSED, f"all answer pages — {hw_label}"
