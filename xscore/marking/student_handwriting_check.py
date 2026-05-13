"""Step 15: per-scan-page closed-vocabulary matcher + handwriting detection.

Public entry point: :func:`check_student_handwriting`.

Given the catalog produced by step 14's
:func:`xscore.marking.empty_exam_page_classifier.classify_empty_exam_pages`,
asks the vision LLM to MATCH each scan page against one of the known
empty-exam page types and one of the known empty-exam page numbers (plus an
N+3 overflow buffer). Also detects student handwriting in the same call.

The out-of-order recheck logic (the two-image compare passes that run when
the primary matcher reports a mismatch versus geometry) lives in the sibling
module :mod:`xscore.marking._handwriting_recheck`.

Refactored out of ``blank_page_detection``. ``BlankCheckStatus`` and the
vision-client helpers live in :mod:`xscore.marking._blank_page_vision_client`.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from pydantic import BaseModel

from eXercise.api_retry import retry_api_call
from xscore.config import (
    HANDWRITING_CHECK_JPEG_DPI,
    HANDWRITING_CHECK_JPEG_QUALITY,
    HANDWRITING_CHECK_RECHECK_JPEG_DPI,
    HANDWRITING_CHECK_RECHECK_JPEG_QUALITY,
    HANDWRITING_CHECK_RECHECK_MODEL,
)
from xscore.marking._blank_page_vision_client import (
    BlankCheckStatus,
    _ClientState,
    _build_client_state,
    _coerce_conf,
    _render_page_jpeg,
)
from xscore.marking._handwriting_recheck import (
    apply_out_of_order_recheck,
    summarise_recheck2,
)


# ─────────── Response parser ────────────────────────────────────────────────


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


# ─────────── Per-scan-page matcher ──────────────────────────────────────────


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
    request_timeout: "httpx.Timeout | None" = None,
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


def _match_scan_page(
    state: _ClientState,
    model_id: str,
    jpeg_bytes: bytes,
    prompt_text: str,
    save_path: Path | None,
    *,
    max_tokens: int,
    request_timeout: "httpx.Timeout | None" = None,
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
                request_timeout=request_timeout,
            ),
            label="Scan-page match",
        )
    except Exception:
        return None, None, None, None, None, None, ""
    save_response(save_path, raw, thinking=thinking_text)
    save_output_data(save_path, raw, ext="json")
    return _parse_scan_match(raw)


# ─────────── Prompt builder + small pure helpers ────────────────────────────


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


def _match_summary(
    exam_page: int,
    page_type: str | None,
    page_number: int | str | None,
) -> tuple[str, str, bool | None]:
    """Compute (page_type label, page_number label, match flag) for display + match column.

    ``match`` is ``True``/``False`` vs the geometry-expected ``exam_page``; ``None``
    when there isn't enough info to decide. Pure function — no closure state.
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


def _post_validate(
    page_type: str | None,
    page_number: int | str | None,
    problem: str,
    *,
    page_type_options: list[str],
    page_number_options_set: set[int],
) -> tuple[str | None, int | str | None, str]:
    """Validate against closed vocabularies + cross-field constraint.

    Doesn't auto-correct — appends notes to *problem* so the artifact captures
    the exact mismatch. Out-of-vocabulary picks become ``None``.
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


# ─────────── Public entry point ──────────────────────────────────────────────


def check_student_handwriting(
    scan_pdf: Path,
    artifact_dir: Path | None = None,
    *,
    cover_page_mode: bool = False,
    pages_per_student: int = 0,
    cover_offset: int = 0,
    empty_exam_classifications: list[dict] | None = None,
    empty_exam_pdf: Path | None = None,
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

    For out-of-order pages, two recheck compares can run; see
    :func:`xscore.marking._handwriting_recheck.apply_out_of_order_recheck`.
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

    from eXercise.ai_client import make_request_timeout  # noqa: PLC0415
    request_timeout = make_request_timeout("standard")

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

    def post_validate(page_type, page_number, problem):
        return _post_validate(
            page_type, page_number, problem,
            page_type_options=page_type_options,
            page_number_options_set=page_number_options_set,
        )

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
        "recheck_model": HANDWRITING_CHECK_RECHECK_MODEL.strip() or None,
        "recheck_jpeg_dpi": HANDWRITING_CHECK_RECHECK_JPEG_DPI,
        "recheck_jpeg_quality": HANDWRITING_CHECK_RECHECK_JPEG_QUALITY,
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
            request_timeout=request_timeout,
        )
        dur = format_duration(time.perf_counter() - t0)
        return idx, scan_page, exam_page, page_type, page_number, hw, conf_pt, conf_pn, conf_hw, problem, dur

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
            page_type, page_number, problem = post_validate(page_type, page_number, problem)
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
            pt, pn, prob = post_validate(pt, pn, prob)
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

    # ── Out-of-order recheck: scan vs expected empty-exam page + recheck2 ───
    rechecked_pages, recheck2_status = apply_out_of_order_recheck(
        results=results,
        scan_pdf=scan_pdf,
        empty_exam_pdf=empty_exam_pdf,
        artifact_dir=artifact_dir,
        empty_exam_classifications=empty_exam_classifications,
        request_timeout=request_timeout,
        max_tok_fallback=max_tok,
        page_width=page_width,
        jpeg_dir=jpeg_dir,
        post_validate=post_validate,
        match_summary=_match_summary,
    )
    summarise_recheck2(recheck2_status)

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
            "rechecked": scan_page in rechecked_pages,
            "rechecked_against_detected": recheck2_status.get(scan_page),
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
