"""Out-of-order recheck for the student_handwriting_check student handwriting check.

When the primary matcher (in :mod:`xscore.marking.student_handwriting_check`)
reports a page-number mismatch versus the geometry-derived expected page,
this module runs two follow-up two-image compares:

- **recheck1**: scan page vs. the empty-exam page the geometry says we expected.
- **recheck2**: when recheck1 says "differs", scan page vs. the empty-exam page
  matching the AI-detected page number (helps distinguish "out of order but
  pages are correctly identified" from "AI hallucinated the page number").

Extracted into its own module so :mod:`xscore.marking.student_handwriting_check`
stays under the 500-line guideline. The :func:`apply_out_of_order_recheck`
entry point mutates ``results`` in place and returns the per-scan-page
recheck outcomes (set of rechecked pages + recheck2 status dict).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from eXercise.api_retry import retry_api_call
from xscore.config import (
    HANDWRITING_CHECK_RECHECK_JPEG_DPI,
    HANDWRITING_CHECK_RECHECK_JPEG_QUALITY,
    HANDWRITING_CHECK_RECHECK_MODEL,
)
from xscore.marking._blank_page_vision_client import (
    _ClientState,
    _build_client_state,
    _coerce_conf,
    _render_page_jpeg,
)


# ─────────── Response parser + schema ───────────────────────────────────────


def _parse_compare(raw: str) -> tuple[bool | None, int | None, str]:
    """Parse the student_handwriting_check recheck two-image comparison response.

    Returns ``(same, confidence, reason)``. Fields parse independently;
    a malformed one does not poison the others.
    """
    if not raw:
        return None, None, ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, None, ""
    if not isinstance(data, dict):
        return None, None, ""

    same_raw = data.get("same_page")
    same: bool | None = same_raw if isinstance(same_raw, bool) else None

    conf = _coerce_conf(data.get("confidence"))

    reason_raw = data.get("reason")
    reason = str(reason_raw).strip() if isinstance(reason_raw, str) else ""
    return same, conf, reason


class _PageCompareResp(BaseModel):
    """Structured-output schema for the student_handwriting_check recheck comparison call (Gemini path)."""
    same_page: bool | None = None
    confidence: int = 5
    reason: str = ""


# ─────────── Two-image compare call ─────────────────────────────────────────


def _call_page_compare(
    state: _ClientState,
    prompt_text: str,
    model_id: str,
    empty_jpeg: bytes,
    scan_jpeg: bytes,
    *,
    max_tokens: int,
    request_timeout: "httpx.Timeout | None" = None,
) -> tuple[str, str]:
    """Two-image comparison call. Image 1 is the empty-exam page; image 2 is the scan page."""
    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import build_gemini_thinking_config, split_gemini_response

        resp = state.gai.models.generate_content(
            model=model_id,
            contents=[
                gai_types.Part.from_bytes(data=empty_jpeg, mime_type="image/jpeg"),
                gai_types.Part.from_bytes(data=scan_jpeg, mime_type="image/jpeg"),
                gai_types.Part.from_text(text=prompt_text),
            ],
            config=gai_types.GenerateContentConfig(
                max_output_tokens=max_tokens,
                response_mime_type="application/json",
                response_schema=_PageCompareResp,
                thinking_config=build_gemini_thinking_config(0),
            ),
        )
        return split_gemini_response(resp)

    import base64 as _base64

    from eXercise.ai_client import build_completion_kwargs

    _use_stream, kw = build_completion_kwargs(state.provider, 0, max_tokens)
    _timeout_kw: dict = {"timeout": request_timeout} if request_timeout is not None else {}
    e_b64 = _base64.b64encode(empty_jpeg).decode()
    s_b64 = _base64.b64encode(scan_jpeg).decode()
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{e_b64}"}},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{s_b64}"}},
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


def _compare_pages(
    state: _ClientState,
    model_id: str,
    empty_jpeg: bytes,
    scan_jpeg: bytes,
    prompt_text: str,
    save_path: Path | None,
    *,
    max_tokens: int,
    request_timeout: "httpx.Timeout | None" = None,
) -> tuple[bool | None, int | None, str]:
    """student_handwriting_check recheck two-image comparison. Returns ``(same, confidence, reason)``."""
    from xscore.shared.prompt_logger import (
        attachment_part, save_output_data, save_prompt, save_response,
    )

    save_prompt(
        save_path, model=model_id,
        messages=[{"role": "user", "content": [
            attachment_part(empty_jpeg, "image/jpeg"),
            attachment_part(scan_jpeg, "image/jpeg"),
            {"type": "text", "text": prompt_text},
        ]}],
    )
    try:
        raw, thinking_text = retry_api_call(
            lambda: _call_page_compare(
                state, prompt_text, model_id, empty_jpeg, scan_jpeg, max_tokens=max_tokens,
                request_timeout=request_timeout,
            ),
            label="Recheck page-compare",
        )
    except Exception:
        return None, None, ""
    save_response(save_path, raw, thinking=thinking_text)
    save_output_data(save_path, raw, ext="json")
    return _parse_compare(raw)


# ─────────── Out-of-order recheck orchestration ─────────────────────────────


def apply_out_of_order_recheck(
    *,
    results: list,
    scan_pdf: Path,
    empty_exam_pdf: Path,
    artifact_dir: Path,
    empty_exam_classifications: list[dict],
    request_timeout,
    max_tok_fallback: int,
    page_width: int,
    jpeg_dir: Path,
    post_validate: Callable,
    match_summary: Callable,
) -> tuple[set[int], dict[int, bool | None]]:
    """Run the two-pass out-of-order recheck and update ``results`` in place.

    Triggered when the primary matcher reports a page-number mismatch versus
    the geometry-derived expected page. For each such page:

    - **recheck1** compares the scan page against the empty-exam page at the
      expected page number. If "same", the page is recovered (its result row
      gets its expected ``ep`` written back as ``page_number``); if "differs",
      a note is appended and recheck2 may run.
    - **recheck2** (only when recheck1 said "differs" and the AI's detected
      page number disagrees with the expected one) compares the scan page
      against the empty-exam page at the AI-detected page number. The outcome
      goes into the returned ``recheck2_status`` dict.

    The terminal output (info / warn / ok lines) is emitted directly from this
    function via the standard ``terminal_ui`` helpers.

    Parameters mirror the closure state that the original inline block read
    from. ``post_validate`` and ``match_summary`` are passed in as callables
    so this module doesn't have to know about the per-call options vocabulary.

    Returns ``(rechecked_pages, recheck2_status)``.
    """
    from eXercise.ai_client import parse_model_spec
    from xscore.shared.exam_paths import artifact_handwriting_prompt_path
    from xscore.shared.terminal_ui import (
        format_duration,
        info_line,
        ok_line,
        warn_line,
    )

    recheck_spec = HANDWRITING_CHECK_RECHECK_MODEL.strip()
    out_of_order_idx: list[int] = []
    if recheck_spec and empty_exam_pdf is not None:
        for i, r in enumerate(results):
            sp_r, ep_r, pt_r, pn_r, *_ = r
            _, _, m = match_summary(ep_r, pt_r, pn_r)
            if m is False:
                out_of_order_idx.append(i)

    rechecked_pages: set[int] = set()
    recheck2_status: dict[int, bool | None] = {}  # scan_page → True/False/None
    if not out_of_order_idx:
        return rechecked_pages, recheck2_status

    # Map printed page number → empty-exam PDF page index, using classify_empty_exam_pages's catalog.
    pn_to_pdf_page: dict[int, int] = {
        p["page_number"]: p["page"]
        for p in (empty_exam_classifications or [])
        if isinstance(p.get("page_number"), int) and isinstance(p.get("page"), int)
    }
    rmodel_id, _rthink, rmax_tok_env = parse_model_spec(recheck_spec)
    rmax_tok = rmax_tok_env or max_tok_fallback
    rclient_or_err = _build_client_state(rmodel_id)
    if isinstance(rclient_or_err, str):
        warn_line(
            f"Skipping out-of-order recheck ({len(out_of_order_idx)} page"
            f"{'s' if len(out_of_order_idx) != 1 else ''}): {rclient_or_err}"
        )
        return rechecked_pages, recheck2_status

    rstate = rclient_or_err
    from xscore.prompts.loader import load_prompt  # noqa: PLC0415
    _, compare_prompt = load_prompt("student_handwriting_check_compare")
    info_line(
        f"Re-checking {len(out_of_order_idx)} out-of-order page"
        f"{'s' if len(out_of_order_idx) != 1 else ''} via empty-exam comparison "
        f"({rmodel_id} @ {HANDWRITING_CHECK_RECHECK_JPEG_DPI} DPI / q{HANDWRITING_CHECK_RECHECK_JPEG_QUALITY}) …"
    )
    for i in out_of_order_idx:
        sp_orig, ep, pt_cur, pn_cur, h_cur, c_pt_cur, c_pn_cur, c_hw_cur, prob_cur = results[i]
        pdf_page = pn_to_pdf_page.get(ep)
        if ep == 0 or pdf_page is None:
            # cover, or classify_empty_exam_pages didn't classify a page with this printed number → skip
            continue
        try:
            scan_jpeg = _render_page_jpeg(
                scan_pdf, sp_orig,
                dpi=HANDWRITING_CHECK_RECHECK_JPEG_DPI,
                quality=HANDWRITING_CHECK_RECHECK_JPEG_QUALITY,
            )
            empty_jpeg = _render_page_jpeg(
                empty_exam_pdf, pdf_page,
                dpi=HANDWRITING_CHECK_RECHECK_JPEG_DPI,
                quality=HANDWRITING_CHECK_RECHECK_JPEG_QUALITY,
            )
        except Exception as e:  # noqa: BLE001
            warn_line(f"  ↳ recheck page {sp_orig}: render failed ({e}); skipping")
            continue
        (jpeg_dir / f"page_{sp_orig:03d}_order_recheck_scan.jpg").write_bytes(scan_jpeg)
        (jpeg_dir / f"page_{sp_orig:03d}_order_recheck_empty.jpg").write_bytes(empty_jpeg)
        save_path = artifact_handwriting_prompt_path(
            artifact_dir, f"page_{sp_orig:03d}_order_recheck"
        )
        t0 = time.perf_counter()
        same, conf_cmp, reason = _compare_pages(
            rstate, rmodel_id, empty_jpeg, scan_jpeg,
            compare_prompt, save_path, max_tokens=rmax_tok,
            request_timeout=request_timeout,
        )
        dur2 = format_duration(time.perf_counter() - t0)

        if same is True:
            pt_new, pn_new, prob_new = post_validate(pt_cur, ep, prob_cur)
            status = f"same as exam pg {ep}"
        elif same is False:
            pt_new, pn_new = pt_cur, pn_cur
            reason_short = (reason or "").strip()
            if len(reason_short) > 60:
                reason_short = reason_short[:59].rstrip() + "…"
            addendum = f"rechecked: differs from empty exam page {ep}"
            if reason_short:
                addendum += f": {reason_short}"
            prob_new = f"{prob_cur}; {addendum}" if prob_cur else addendum
            status = f"differs from exam pg {ep}"
        else:
            pt_new, pn_new = pt_cur, pn_cur
            addendum = "recheck inconclusive"
            prob_new = f"{prob_cur}; {addendum}" if prob_cur else addendum
            status = "inconclusive"

        # ── Second recheck: scan page vs empty exam at AI-detected page number ─
        detected_pn: int | None = pn_cur if isinstance(pn_cur, int) else None
        if same is False and detected_pn is not None and detected_pn != ep:
            detected_pdf_page = pn_to_pdf_page.get(detected_pn)
            if detected_pdf_page is not None:
                try:
                    empty_jpeg2 = _render_page_jpeg(
                        empty_exam_pdf, detected_pdf_page,
                        dpi=HANDWRITING_CHECK_RECHECK_JPEG_DPI,
                        quality=HANDWRITING_CHECK_RECHECK_JPEG_QUALITY,
                    )
                except Exception as e:  # noqa: BLE001
                    warn_line(f"  ↳ recheck2 page {sp_orig}: render failed ({e}); skipping")
                else:
                    (jpeg_dir / f"page_{sp_orig:03d}_order_recheck2_empty.jpg").write_bytes(empty_jpeg2)
                    save_path2 = artifact_handwriting_prompt_path(
                        artifact_dir, f"page_{sp_orig:03d}_order_recheck2"
                    )
                    t1 = time.perf_counter()
                    same2, conf2, reason2 = _compare_pages(
                        rstate, rmodel_id, empty_jpeg2, scan_jpeg,
                        compare_prompt, save_path2, max_tokens=rmax_tok,
                        request_timeout=request_timeout,
                    )
                    dur3 = format_duration(time.perf_counter() - t1)
                    if same2 is True:
                        recheck2_status[sp_orig] = True
                        addendum2 = f"recheck2: confirmed as detected pg {detected_pn} (out of order)"
                        status2 = f"same as detected pg {detected_pn}"
                    elif same2 is False:
                        recheck2_status[sp_orig] = False
                        reason2_short = (reason2 or "").strip()
                        if len(reason2_short) > 60:
                            reason2_short = reason2_short[:59].rstrip() + "…"
                        addendum2 = f"recheck2: differs from detected pg {detected_pn}"
                        if reason2_short:
                            addendum2 += f": {reason2_short}"
                        status2 = f"differs from detected pg {detected_pn}"
                    else:
                        recheck2_status[sp_orig] = None
                        addendum2 = f"recheck2: inconclusive (vs detected pg {detected_pn})"
                        status2 = "inconclusive"
                    prob_new = f"{prob_new}; {addendum2}" if prob_new else addendum2
                    conf_str2 = f"conf={conf2}" if conf2 is not None else "conf=?"
                    # When recheck2 confirms the AI's original page-number
                    # was correct (the scan is just physically misordered),
                    # the line is good news — keep it ok_line. Disagreement
                    # or inconclusive results stay at warn level.
                    line_fn2 = ok_line if recheck2_status.get(sp_orig) is True else warn_line
                    line_fn2(
                        f"  ↳ recheck2 page {sp_orig:>{page_width}d}  ·  {status2:<24}"
                        f"  ·  pg {detected_pn:<5}  ·  {conf_str2}  ·  {dur3}"
                    )

        _, pn_label, match_after = match_summary(ep, pt_new, pn_new)
        line_fn = ok_line if (match_after is True and not prob_new) else warn_line
        conf_str = f"conf={conf_cmp}" if conf_cmp is not None else "conf=?"
        line_fn(
            f"  ↳ recheck page {sp_orig:>{page_width}d}  ·  {status:<24}"
            f"  ·  pg {pn_label:<5}  ·  {conf_str}  ·  {dur2}"
        )
        results[i] = (sp_orig, ep, pt_new, pn_new, h_cur, c_pt_cur, c_pn_cur, c_hw_cur, prob_new)
        rechecked_pages.add(sp_orig)

    return rechecked_pages, recheck2_status


def summarise_recheck2(recheck2_status: dict[int, bool | None]) -> None:
    """Emit the recheck2 summary line(s) (no-op if nothing to say)."""
    if not recheck2_status:
        return
    from xscore.shared.terminal_ui import info_line, warn_line

    confirmed = sum(1 for v in recheck2_status.values() if v is True)
    contradicted = sum(1 for v in recheck2_status.values() if v is False)
    inconclusive = sum(1 for v in recheck2_status.values() if v is None)
    total = len(recheck2_status)
    if contradicted == 0 and inconclusive == 0:
        info_line(
            f"Recheck2: all {total} out-of-order page"
            f"{'s' if total != 1 else ''} confirmed against AI-detected page "
            "numbers — scan appears misordered but page identities are reliable."
        )
    else:
        parts = [f"{confirmed} confirmed"]
        if contradicted:
            parts.append(f"{contradicted} contradicted")
        if inconclusive:
            parts.append(f"{inconclusive} inconclusive")
        warn_line(
            f"Recheck2: {', '.join(parts)} of {total} out-of-order page"
            f"{'s' if total != 1 else ''}."
        )
