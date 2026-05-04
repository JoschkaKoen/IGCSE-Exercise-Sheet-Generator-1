"""Steps 14 + 15: classify empty-exam pages, then check student scans for handwriting.

Step 14 (classify_empty_exam_pages): vision LLM call per empty-exam page. Picks
a ``page_type`` from the closed vocabulary
{cover|instruction|question|blank|writing-space} and reads the printed page
number. Writes ``14_empty_exam_classification/empty_exam_classifications.json``.

Step 15 (student_handwriting_check): vision LLM call per scan page. Matches
each scan page against the catalog produced by step 14 (page type + page
number, plus an N+3 overflow buffer) and detects student handwriting in the
same call. Writes ``15_student_handwriting/handwriting.json``.

Both functions emit per-page / per-task progress lines via the terminal_ui
``info_line`` / ``ok_line`` / ``warn_line`` helpers. Policy stays at the
dispatcher: INCONCLUSIVE returns from these functions; the dispatcher in
``xscore/steps/geometry.py`` decides warn-vs-SystemExit based on the per-step
``*_STRICT`` env var.

Pages where the handwriting flag could not be determined are listed under a
sibling ``inconclusive_pages`` field in the step-15 artifact so the
register-builder in ``xscore/marking/marking_page_register.py`` can omit them
from skip / extras decisions.
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
from xscore.config import HANDWRITING_CHECK_JPEG_DPI, HANDWRITING_CHECK_JPEG_QUALITY


# ─────────── Image extraction ───────────────────────────────────────────────

HANDWRITING_JPEG_DPI = 150
HANDWRITING_JPEG_QUALITY = 75  # PyMuPDF default for tobytes("jpeg") — explicit so it's announceable


def _render_page_jpeg(
    pdf_path: Path,
    page_1based: int,
    dpi: int = HANDWRITING_JPEG_DPI,
    quality: int = HANDWRITING_JPEG_QUALITY,
) -> bytes:
    import fitz
    with fitz.open(str(pdf_path)) as doc:
        pix = doc[page_1based - 1].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    return pix.tobytes("jpeg", jpg_quality=quality)


def _extract_page_as_pdf_bytes(pdf_path: Path, page_1based: int) -> bytes:
    """Extract one page out of *pdf_path* as a self-contained single-page PDF.

    Used by the step-14 empty-exam classifier on the Gemini path so each
    parallel call sees exactly one page (rather than the whole exam) without
    rasterizing the vector PDF first.
    """
    import io

    import fitz

    with fitz.open(str(pdf_path)) as src:
        out = fitz.open()
        try:
            out.insert_pdf(src, from_page=page_1based - 1, to_page=page_1based - 1)
            buf = io.BytesIO()
            out.save(buf)
            return buf.getvalue()
        finally:
            out.close()


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

def _coerce_conf(v) -> int | None:
    """Coerce a model-returned confidence to an int in [0, 10], or None on garbage."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return max(0, min(10, v))
    if isinstance(v, (float, str)):
        try:
            return max(0, min(10, int(float(str(v).strip()))))
        except (TypeError, ValueError):
            return None
    return None


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


def _parse_scan_match(
    raw: str,
) -> tuple[str | None, int | str | None, bool | None, int | None, int | None, int | None, str]:
    """Parse the step-15 scan-page matcher response.

    Returns
    ``(page_type, page_number, has_handwriting, conf_page_type, conf_page_number,
       conf_handwriting, problem)``.

    ``page_number`` is an int when the model picked a page-number entry, the
    string ``"cover"`` or ``"none"`` when it picked one of those special tokens,
    or ``None`` if the field was missing/malformed. The caller is responsible
    for validating it against the closed option list.
    """
    empty: tuple[str | None, int | str | None, bool | None, int | None, int | None, int | None, str] = (
        None, None, None, None, None, None, ""
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
    page_number: int | str | None = None
    if isinstance(pn_raw, bool):
        page_number = None
    elif isinstance(pn_raw, int):
        page_number = pn_raw
    elif isinstance(pn_raw, str):
        s = pn_raw.strip()
        sl = s.lower()
        if sl in ("cover", "none"):
            page_number = sl
        elif s and sl not in ("null",):
            try:
                page_number = int(s)
            except ValueError:
                page_number = None
    if isinstance(page_number, int) and (page_number <= 0 or page_number > 999):
        page_number = None

    hw_raw = data.get("has_handwriting")
    if hw_raw is None:
        hw_raw = data.get("answer")  # legacy
    hw: bool | None = hw_raw if isinstance(hw_raw, bool) else None

    conf_pt = _coerce_conf(data.get("confidence_page_type"))
    conf_pn = _coerce_conf(data.get("confidence_page_number"))
    conf_hw = _coerce_conf(data.get("confidence_handwriting"))

    problem_raw = data.get("problem")
    problem = str(problem_raw).strip() if isinstance(problem_raw, str) else ""
    return page_type, page_number, hw, conf_pt, conf_pn, conf_hw, problem


# ─────────── Step 14: classify each empty-exam page ──────────────────────────


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
    b64 = _base64.b64encode(jpeg_bytes).decode()
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": prompt_text},
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


def _classify_empty_page(
    state: _ClientState,
    model_id: str,
    pdf_bytes: bytes | None,
    jpeg_bytes: bytes | None,
    save_path: Path | None,
    *,
    max_tokens: int,
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
            ),
            label="Empty-exam page classification",
        )
    except Exception:
        return None, None, None, None, ""
    save_response(save_path, raw, thinking=thinking_text)
    save_output_data(save_path, raw, ext="json")
    return _parse_empty_exam_class(raw)


# ─────────── Step 15: per-scan-page matcher ─────────────────────────────────


class _ScanPageMatchResp(BaseModel):
    """Structured-output schema for the step-15 scan-page matcher (Gemini path).

    ``page_number`` is a string here (rather than a union) because the Gemini
    structured-output schema rejects unions. The OpenAI-compat path reads the
    same JSON shape; the parser :func:`_parse_scan_match` accepts both string
    digits and the special tokens ``"cover"`` / ``"none"``.
    """
    page_type: str | None = None
    page_number: str | None = None
    has_handwriting: bool | None = None
    confidence_page_type: int = 5
    confidence_page_number: int = 5
    confidence_handwriting: int = 5
    problem: str = ""


def _call_scan_match(
    state: _ClientState,
    prompt_text: str,
    model_id: str,
    jpeg_bytes: bytes,
    *,
    max_tokens: int,
) -> tuple[str, str]:
    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import build_gemini_thinking_config, split_gemini_response

        resp = state.gai.models.generate_content(
            model=model_id,
            contents=[
                gai_types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"),
                gai_types.Part.from_text(text=prompt_text),
            ],
            config=gai_types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
                response_schema=_ScanPageMatchResp,
                thinking_config=build_gemini_thinking_config(0),
            ),
        )
        return split_gemini_response(resp)

    import base64 as _base64

    from eXercise.ai_client import build_completion_kwargs

    _use_stream, kw = build_completion_kwargs(state.provider, 0, max_tokens)
    b64 = _base64.b64encode(jpeg_bytes).decode()
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        {"type": "text", "text": prompt_text},
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


def _match_scan_page(
    state: _ClientState,
    model_id: str,
    jpeg_bytes: bytes,
    prompt_text: str,
    save_path: Path | None,
    *,
    max_tokens: int,
) -> tuple[str | None, int | str | None, bool | None, int | None, int | None, int | None, str]:
    """Step-15 per-scan-page call. Returns the parsed
    ``(page_type, page_number, has_handwriting, conf_pt, conf_pn, conf_hw, problem)``
    tuple; all components are independent and may be ``None`` on parse failure.
    """
    from xscore.shared.prompt_logger import (
        attachment_part, save_output_data, save_prompt, save_response,
    )

    save_prompt(
        save_path, model=model_id,
        messages=[{"role": "user", "content": [
            attachment_part(jpeg_bytes, "image/jpeg"),
            {"type": "text", "text": prompt_text},
        ]}],
    )
    try:
        raw, thinking_text = retry_api_call(
            lambda: _call_scan_match(
                state, prompt_text, model_id, jpeg_bytes, max_tokens=max_tokens,
            ),
            label="Scan-page match",
        )
    except Exception:
        return None, None, None, None, None, None, ""
    save_response(save_path, raw, thinking=thinking_text)
    save_output_data(save_path, raw, ext="json")
    return _parse_scan_match(raw)


# ─────────── Public entry points ──────────────────────────────────────────────


PAGE_TYPE_VOCABULARY: tuple[str, ...] = (
    "cover page",
    "instruction page",
    "question page",
    "blank page",
    "writing space page",
)


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


def _build_match_prompt(
    page_type_options: list[str],
    page_number_options: list[int],
    n_max_seen: int,
) -> str:
    """Format the v8 student_handwriting_check.md template with closed-vocab options."""
    from xscore.prompts.loader import load_prompt

    page_type_block = "\n".join(f"- `{t}`" for t in page_type_options)
    pn_lines: list[str] = []
    for n in page_number_options:
        if n_max_seen and n > n_max_seen:
            pn_lines.append(f"- `{n}` (overflow buffer; not seen in empty exam)")
        else:
            pn_lines.append(f"- `{n}`")
    page_number_block = "\n".join(pn_lines) if pn_lines else "- (no integer page numbers detected in empty exam)"
    _, prompt = load_prompt(
        "student_handwriting_check",
        page_type_options=page_type_block,
        page_number_options=page_number_block,
    )
    return prompt.rstrip("\n")


def check_student_handwriting(
    scan_pdf: Path,
    artifact_dir: Path | None = None,
    *,
    cover_page_mode: bool = False,
    pages_per_student: int = 0,
    cover_offset: int = 0,
    empty_exam_classifications: list[dict] | None = None,
) -> tuple[BlankCheckStatus, str | None]:
    """Step 15: per-scan-page closed-vocabulary matcher.

    Given the catalog produced by step 14's :func:`classify_empty_exam_pages`,
    asks the vision LLM to MATCH each scan page against one of the known
    empty-exam page types and one of the known empty-exam page numbers (plus
    an N+3 overflow buffer). Also detects student handwriting in the same
    call.

    Writes ``15_student_handwriting/handwriting.json`` containing the
    ``scan_pages`` block. Returns ``(BlankCheckStatus, message)``; never
    raises (dispatcher policy).
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

    model_id, _thinking, max_tok_env = parse_model_spec(
        os.environ.get("HANDWRITING_CHECK_MODEL", "qwen3-vl-flash")
    )
    client_or_err = _build_client_state(model_id)
    if isinstance(client_or_err, str):
        return BlankCheckStatus.INCONCLUSIVE, client_or_err
    state = client_or_err

    # Build closed vocabularies from step 14's catalog.
    empty_exam_classifications = empty_exam_classifications or []
    page_type_set = {
        p["page_type"] for p in empty_exam_classifications
        if p.get("page_type")
    }
    page_type_set.add("cover page")  # always available even if step 14 missed it
    page_type_options = sorted(page_type_set)

    page_numbers_seen = sorted({
        p["page_number"] for p in empty_exam_classifications
        if isinstance(p.get("page_number"), int)
    })
    n_max = max(page_numbers_seen, default=0)
    page_number_options = page_numbers_seen + [n_max + 1, n_max + 2, n_max + 3]
    page_number_options_set: set[int] = set(page_number_options)

    prompt_text = _build_match_prompt(page_type_options, page_number_options, n_max)
    max_tok = max_tok_env or 192

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

    tasks: list[tuple[int, int]] = []  # (scan_page, exam_page)
    for scan_page in range(1, scan_n_pages + 1):
        exam_page = _expected(scan_page)
        if exam_page == 0 or exam_page >= 1:
            tasks.append((scan_page, exam_page))

    metadata = {
        "scan_pdf_page_count": scan_n_pages,
        "cover_page_mode": cover_page_mode,
        "pages_per_student": pages_per_student,
        "cover_offset": cover_offset,
        "model": model_id,
        "page_type_options": page_type_options,
        "page_number_options": page_number_options,
    }

    if not tasks:
        artifact = {
            "metadata": metadata,
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
        f"Matching {len(tasks)} scan pages against empty exam "
        f"({len(page_type_options)} page types · {len(page_number_options)} page numbers + 'cover'/'none') …"
    )

    def _match_summary(
        exam_page: int,
        page_type: str | None,
        page_number: int | str | None,
    ) -> tuple[str, str, bool | None]:
        """Compute (page_type label, page_number label, match flag) for display + match column.

        match: True/False vs the geometry-expected exam_page; None when not enough info.
        """
        expected_is_cover = (exam_page == 0)
        # Display labels
        pt_label = page_type if page_type is not None else "?"
        if page_number == "cover":
            pn_label = "cover"
        elif page_number == "none":
            pn_label = "—"
        elif isinstance(page_number, int):
            pn_label = str(page_number)
        else:
            pn_label = "?"
        # Match logic — derive from page_number when available, fall back to page_type.
        if expected_is_cover:
            if page_number == "cover" or page_type == "cover page":
                match: bool | None = True
            elif page_number is None and page_type is None:
                match = None
            else:
                match = False
        else:
            if page_number == "cover":
                match = False
            elif page_number == "none":
                match = None
            elif isinstance(page_number, int):
                match = (page_number == exam_page)
            else:
                match = None
        return pt_label, pn_label, match

    def _match_one(
        idx: int, args: tuple, suffix: str = ""
    ) -> tuple[int, int, int, str | None, int | str | None, bool | None,
               int | None, int | None, int | None, str, str]:
        """Returns (idx, scan_page, exam_page, page_type, page_number, has_handwriting,
                    conf_pt, conf_pn, conf_hw, problem, dur_str)."""
        scan_page, exam_page = args
        jpeg_bytes = _render_page_jpeg(
            scan_pdf,
            scan_page,
            dpi=HANDWRITING_CHECK_JPEG_DPI,
            quality=HANDWRITING_CHECK_JPEG_QUALITY,
        )
        (jpeg_dir / f"page_{scan_page:03d}.jpg").write_bytes(jpeg_bytes)
        save_path = artifact_handwriting_prompt_path(
            artifact_dir, f"page_{scan_page:03d}{suffix}"
        )
        t0 = time.perf_counter()
        page_type, page_number, hw, conf_pt, conf_pn, conf_hw, problem = _match_scan_page(
            state, model_id, jpeg_bytes, prompt_text, save_path, max_tokens=max_tok,
        )
        dur = format_duration(time.perf_counter() - t0)
        return idx, scan_page, exam_page, page_type, page_number, hw, conf_pt, conf_pn, conf_hw, problem, dur

    def _post_validate(
        page_type: str | None,
        page_number: int | str | None,
        problem: str,
    ) -> tuple[str | None, int | str | None, str]:
        """Validate against closed vocabularies + cross-field constraint.

        Doesn't auto-correct — appends notes to *problem* so the artifact
        captures the exact mismatch. Out-of-vocabulary picks become None.
        """
        notes: list[str] = []
        if page_type is not None and page_type not in page_type_options:
            notes.append(f"page_type {page_type!r} not in options")
            page_type = None
        if isinstance(page_number, int) and page_number not in page_number_options_set:
            notes.append(f"page_number {page_number} not in options")
            page_number = None
        if page_number == "cover" and page_type not in (None, "cover page"):
            notes.append(f"cross-field mismatch: page_number='cover' vs page_type={page_type!r}")
        if page_type == "cover page" and isinstance(page_number, int):
            notes.append(f"cross-field mismatch: page_type='cover page' but page_number={page_number}")
        if notes:
            extra = "; ".join(notes)
            problem = f"{problem}; {extra}" if problem else extra
        return page_type, page_number, problem

    def _emit(
        scan_page: int,
        exam_page: int,
        page_type: str | None,
        page_number: int | str | None,
        hw: bool | None,
        conf_pt: int | None,
        conf_pn: int | None,
        conf_hw: int | None,
        problem: str,
        dur: str,
    ) -> None:
        pt_label, pn_label, match = _match_summary(exam_page, page_type, page_number)
        # Handwriting label
        if hw is None:
            hw_label = "?  hw=?   "
        elif hw:
            hw_label = "✓  hw     "
        else:
            hw_label = "x  no hw  "
        line_fn = warn_line if (match is False or problem) else ok_line
        confs = [c for c in (conf_pt, conf_pn, conf_hw) if c is not None]
        conf_min = min(confs) if confs else None
        conf_str = f"conf={conf_min}" if conf_min is not None else "conf=?"
        problem_suffix = ""
        if problem:
            p_short = problem if len(problem) <= 120 else problem[:119].rstrip() + "…"
            problem_suffix = f"  ·  {p_short}"
        line_fn(
            f"Page {scan_page:>{page_width}d}/{scan_n_pages}"
            f"  ·  {pt_label:<18}  ·  pg {pn_label:<5}  ·  {hw_label}"
            f"  ·  {conf_str:<7}  ·  {dur}{problem_suffix}"
        )

    # results: per-page tuple in scan-page order.
    results: list[tuple[int, int, str | None, int | str | None, bool | None,
                        int | None, int | None, int | None, str]] = []
    pending: dict[int, tuple] = {}
    next_idx = 0
    workers = min(len(tasks), int(os.environ.get("HANDWRITING_WORKERS", "32")))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_match_one, i, t): i for i, t in enumerate(tasks)}
        for fut in as_completed(futs):
            idx, scan_page, exam_page, page_type, page_number, hw, conf_pt, conf_pn, conf_hw, problem, dur = fut.result()
            page_type, page_number, problem = _post_validate(page_type, page_number, problem)
            pending[idx] = (scan_page, exam_page, page_type, page_number, hw, conf_pt, conf_pn, conf_hw, problem, dur)
            while next_idx in pending:
                sp, ep, pt, pn, h, c_pt, c_pn, c_hw, prob, d = pending.pop(next_idx)
                _emit(sp, ep, pt, pn, h, c_pt, c_pn, c_hw, prob, d)
                results.append((sp, ep, pt, pn, h, c_pt, c_pn, c_hw, prob))
                next_idx += 1

    # ── Recheck pass: one retry per page where any primary field came back None ─
    inconclusive_idx = [
        i for i, r in enumerate(results)
        if r[2] is None or r[3] is None or r[4] is None
    ]
    if inconclusive_idx:
        info_line(
            f"Re-checking {len(inconclusive_idx)} inconclusive page"
            f"{'s' if len(inconclusive_idx) != 1 else ''} …"
        )
        for i in inconclusive_idx:
            scan_page, exam_page, *_ = results[i]
            _, sp, ep, pt, pn, h, c_pt, c_pn, c_hw, prob, dur = _match_one(
                i, (scan_page, exam_page), suffix="_recheck"
            )
            pt, pn, prob = _post_validate(pt, pn, prob)
            pt_label, pn_label, match = _match_summary(ep, pt, pn)
            if h is None or pt is None or pn is None:
                line_fn = warn_line
                status = "still inconclusive"
            else:
                status = "ok"
                line_fn = warn_line if (match is False or prob) else ok_line
            confs = [c for c in (c_pt, c_pn, c_hw) if c is not None]
            conf_min = min(confs) if confs else None
            conf_str = f"conf={conf_min}" if conf_min is not None else "conf=?"
            problem_suffix = ""
            if prob:
                p_short = prob if len(prob) <= 120 else prob[:119].rstrip() + "…"
                problem_suffix = f"  ·  {p_short}"
            line_fn(
                f"  ↳ recheck page {sp:>{page_width}d}  ·  {status}"
                f"  ·  {pt_label:<18}  ·  pg {pn_label:<5}  ·  {conf_str}  ·  {dur}{problem_suffix}"
            )
            results[i] = (sp, ep, pt, pn, h, c_pt, c_pn, c_hw, prob)

    # ── Build artifact ───────────────────────────────────────────────────────
    scan_pages_out: list[dict] = []
    inconclusive_pages: list[dict] = []
    mismatch_total: list[tuple[int, str, str]] = []
    for scan_page, exam_page, page_type, page_number, has_hw, conf_pt, conf_pn, conf_hw, problem in results:
        expected_is_cover = (exam_page == 0)
        expected_pn: int | None = None if expected_is_cover else exam_page
        pt_label, pn_label, pn_match = _match_summary(exam_page, page_type, page_number)
        matched_page_number: int | None = page_number if isinstance(page_number, int) else None
        scan_pages_out.append({
            "scan_page": scan_page,
            "expected_is_cover": expected_is_cover,
            "expected_page_number": expected_pn,
            "page_type": page_type,
            "matched_page_number": matched_page_number,
            "has_handwriting": has_hw,
            "confidence_page_type": conf_pt,
            "confidence_page_number": conf_pn,
            "confidence_handwriting": conf_hw,
            "problem": problem,
            "match": pn_match,
        })
        if has_hw is None or page_type is None or page_number is None:
            inconclusive_pages.append({
                "scan_page": scan_page,
                "reason": problem or "matcher returned a missing field",
            })
        if pn_match is False:
            expected_label = "cover" if expected_is_cover else str(exam_page)
            mismatch_total.append((scan_page, expected_label, pn_label))

    artifact = {
        "metadata": metadata,
        "scan_pages": scan_pages_out,
        "inconclusive_pages": inconclusive_pages,
    }
    hw_path = artifact_handwriting_json_path(artifact_dir)
    hw_path.parent.mkdir(parents=True, exist_ok=True)
    hw_path.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    # Tallies — cover pages excluded from handwriting tallies.
    answer_results = [r for r in results if r[1] != 0]
    hw_count = sum(1 for r in answer_results if r[4] is True)
    n_done = sum(1 for r in answer_results if r[4] is not None)
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
