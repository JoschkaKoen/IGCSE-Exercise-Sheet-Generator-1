"""Per-page rendering and AI marking call for the grading pipeline."""

from __future__ import annotations

import base64
import re
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eXercise.ai_client import collect_streamed_response
from eXercise.api_retry import retry_api_call
from xscore.config import MARKING_JPEG_QUALITY
from xscore.marking.formats.base import FormatParseError, MarkingFormat
from xscore.marking.mark_xml import MarkingFailure
from xscore.prompts.loader import load_prompt
from xscore.shared.prompt_logger import save_prompt, save_response
from xscore.shared.terminal_ui import warn_line


def _render_page_b64(doc: Any, page_idx: int, dpi: int = 300) -> str:
    """Render a fitz Document page at *page_idx* as base64 JPEG.

    Fast path: cleaned_scan.pdf (built by deskew_pdf_raster) embeds exactly one
    full-page JPEG per page at the source DPI. When that matches the requested
    DPI within 5 %, return the embedded bytes verbatim — no decode, no raster,
    no re-encode. Slow path (foreign PDFs / explicit DPI override): rasterize
    at *dpi* and JPEG-encode at MARKING_JPEG_QUALITY (which is therefore a
    no-op on the fast path).
    """
    import fitz
    page = doc[page_idx]
    imgs = page.get_images(full=True)
    if len(imgs) == 1:
        info = doc.extract_image(imgs[0][0])
        if info.get("ext") == "jpeg":
            implied_dpi = info["width"] / (page.rect.width / 72)
            if abs(implied_dpi - dpi) / dpi < 0.05:
                return base64.b64encode(info["image"]).decode()
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    return base64.b64encode(pix.tobytes("jpeg", jpg_quality=MARKING_JPEG_QUALITY)).decode()


def _bq_key(bq: dict) -> tuple:
    """Group key for a blueprint question: (bare_number, subpage_row, subpage_col).

    The _N suffix is stripped so Q38 and Q38_2 share the same group; blueprint
    questions consume positionally so Q38 gets group[0] and Q38_2 gets group[1].
    """
    _row = bq.get("subpage_row")
    _col = bq.get("subpage_col")
    num = re.sub(r'_\d+$', '', str(bq.get("number", "")))
    return (
        num,
        int(_row) if _row is not None else 1,
        int(_col) if _col is not None else 1,
    )


def _build_marking_system_prompt(
    blueprint: dict,
    scheme_graphics: "list[tuple[str, int, str]]" = (),
    *,
    has_continuation: bool = False,
    fmt: "MarkingFormat | None" = None,
    is_cs: bool = False,
) -> str:
    """Build the system prompt shared by the JPEG and Gemini PDF marking paths."""
    if fmt is None:
        from xscore.marking.formats.xml_format import XmlMarkingFormat
        fmt = XmlMarkingFormat()
    layout = blueprint.get("layout") or {"rows": 1, "cols": 1}
    rows, cols = int(layout.get("rows", 1)), int(layout.get("cols", 1))

    # --- Sections A + B + C + D: role/task, field rules, output format, format validity ---
    # The per-format ai_marking_<fmt>.md SYSTEM section embeds A, C, D around a
    # $field_rules placeholder. ai_marking_fragments.md FIELD_RULES section (B) is
    # loaded first with $criterion_ref so the assembled system prompt is byte-identical
    # to the pre-consolidation 4-method append.
    _, _b = load_prompt(
        "ai_marking_fragments", section="field_rules", criterion_ref=fmt.criterion_ref(),
    )
    _, system_prompt = load_prompt(
        fmt.prompt_name(), section="system", field_rules=_b.rstrip("\n"),
    )
    system_prompt = system_prompt.rstrip("\n")

    # --- Section E: grid navigation (only for multi-subpage layouts) ---
    if rows > 1 or cols > 1:
        _, _e = load_prompt(
            "ai_marking_fragments",
            section="grid",
            rows=rows,
            cols=cols,
            subpage_ref=fmt.subpage_ref(),
        )
        system_prompt += "\n\n" + _e.rstrip("\n")

    # --- Section F: mark-scheme graphics (only when present) ---
    if scheme_graphics:
        _seen: dict[str, int] = {}
        for _qn, _, _ in scheme_graphics:
            _seen[_qn] = _seen.get(_qn, 0) + 1
        _idx: dict[str, int] = {}
        _lines: list[str] = []
        for _qn, _, _ in scheme_graphics:
            _idx[_qn] = _idx.get(_qn, 0) + 1
            _label = f"image {_idx[_qn]}" if _seen[_qn] > 1 else "image"
            _lines.append(f"  • Question {_qn} expected answer → {_label}")
        _, _f = load_prompt(
            "ai_marking_fragments", section="graphics", graphics_lines="\n".join(_lines),
        )
        system_prompt += "\n\n" + _f.rstrip("\n")

    # --- Section G: continuation pages ---
    if has_continuation:
        _, _g = load_prompt("ai_marking_fragments", section="continuation")
        system_prompt += "\n\n" + _g.rstrip("\n")

    # --- Section H: code formatting (only for Computer Science exams) ---
    if is_cs:
        _, _h = load_prompt("ai_marking_fragments", section="code_formatting")
        system_prompt += "\n\n" + _h.rstrip("\n")

    return system_prompt


def _mark_page(
    client: Any,
    model_id: str,
    b64: str,
    blueprint: dict,
    thinking_kw: dict,
    blueprint_xml: str = "",
    use_stream: bool = False,
    prompt_save_path: Path | None = None,
    warn: Callable[[str], None] = warn_line,
    scheme_graphics: list[tuple[str, int, str]] = (),
    fmt: "MarkingFormat | None" = None,
    extra_b64: list[str] = (),
    reuse_cache: bool = False,
    is_cs: bool = False,
) -> dict:
    """Vision call to fill in a marking blueprint for one scan page.

    Raises :class:`MarkingFailure` if all attempts are exhausted.
    *extra_b64* — additional continuation-page images appended after the main image.
    *reuse_cache* — when True, look up the request in
    :mod:`xscore.shared.response_cache` before calling the API; on miss, store
    the API response after a successful parse. Default False (no cache).
    """
    if fmt is None:
        from xscore.marking.formats.xml_format import XmlMarkingFormat
        fmt = XmlMarkingFormat()
    use_stream = use_stream and fmt.prefer_stream()
    system_prompt = _build_marking_system_prompt(
        blueprint, scheme_graphics, has_continuation=bool(extra_b64), fmt=fmt, is_cs=is_cs,
    )

    _, user_text = load_prompt(fmt.prompt_name(), section="user", blueprint=blueprint_xml)
    _user_content: list[dict] = [
        {"type": "text", "text": user_text},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    for _cb64 in extra_b64:
        _user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_cb64}"}})
    for _, _, _g_b64 in scheme_graphics:
        _user_content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_g_b64}"}})
    kwargs: dict[str, Any] = dict(
        model=model_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _user_content},
        ],
    )
    kwargs.update(thinking_kw)
    kwargs.update(fmt.api_extra_kwargs(model_id))

    save_prompt(prompt_save_path, model=model_id, messages=kwargs["messages"])

    # Optional response cache: only consulted when the user opted in via the
    # NL prompt ("reuse cache"). Key folds in every input that affects the
    # response so a stale hit is impossible by construction.
    _cache_key: str | None = None
    if reuse_cache:
        from xscore.shared.response_cache import cache_key as _cache_key_fn, cache_get
        _extra_hashes = ",".join(extra_b64) + "|" + ",".join(g[2] for g in scheme_graphics)
        try:
            _img_bytes = base64.b64decode(b64) if b64 else b""
        except Exception:
            _img_bytes = b""
        _cache_key = _cache_key_fn(
            model=model_id,
            system_prompt=system_prompt,
            user_prompt=user_text,
            image_bytes=_img_bytes,
            extra=_extra_hashes,
        )
        _hit = cache_get(_cache_key)
        if _hit is not None:
            _cached_raw = _hit.get("response", "")
            if isinstance(_cached_raw, str) and _cached_raw:
                try:
                    return _apply_marking_response(_cached_raw, blueprint, fmt, warn)
                except FormatParseError:
                    # Cached entry is malformed for the current parser — discard it
                    # and fall through to a live call.
                    pass

    def _do_call() -> tuple[str, str]:
        if use_stream:
            # Stream consumed inside the closure so a mid-stream failure retries.
            _th: list[str] = []
            _stream = client.chat.completions.create(**kwargs, stream=True)
            return collect_streamed_response(_stream, thinking_out=_th), "".join(_th)
        _resp = client.chat.completions.create(**kwargs)
        return (
            _resp.choices[0].message.content or "",
            getattr(_resp.choices[0].message, "reasoning_content", "") or "",
        )

    _last_raw: str = ""
    try:
        raw, thinking_text = retry_api_call(_do_call, label=f"Marking ({model_id})")
        _last_raw = raw
        save_response(prompt_save_path, raw, thinking=thinking_text)
        result = _apply_marking_response(raw, blueprint, fmt, warn)
        if reuse_cache and _cache_key is not None:
            from xscore.shared.response_cache import cache_put
            cache_put(_cache_key, model=model_id, response=raw)
        return result
    except FormatParseError as exc:
        warn(f"Marking parse error — marking aborted ({exc})")
        raise MarkingFailure(attempts=1, last_exc=exc, last_raw=_last_raw)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        raise MarkingFailure(attempts=1, last_exc=exc, last_raw=_last_raw)


def _apply_marking_response(
    raw: str,
    blueprint: dict,
    fmt: "MarkingFormat",
    warn: Callable[[str], None],
) -> dict:
    """Parse a raw marking response and apply it to *blueprint*.

    Used by both the live API path and the cache-hit path so the validation /
    clamping / MCQ-fix / blank-answer logic runs identically. Raises
    :class:`FormatParseError` if *raw* is unparseable.
    """
    parsed_questions = fmt.parse_response(raw)
    result = blueprint.copy()
    fill_groups: dict[tuple, list] = defaultdict(list)
    for q in parsed_questions:
        fill_groups[_bq_key(q)].append(q)

    fill_group_idx: dict[tuple, int] = defaultdict(int)
    _unfilled = []
    for bq in result.get("questions", []):
        key = _bq_key(bq)
        idx = fill_group_idx[key]
        fill_group_idx[key] += 1
        group = fill_groups.get(key, [])
        if idx < len(group):
            fq = group[idx]
            bq["student_answer"] = fq['student_answer']
            bq["assigned_marks"] = fq['assigned_marks']
            bq["explanation"] = fq['explanation']
            # `confidence` is optional — present only when item 3 (confidence
            # side artifact) is in effect AND the AI produced it.
            if "confidence" in fq:
                bq["confidence"] = fq["confidence"]
        else:
            _unfilled.append(bq.get("number"))

    if _unfilled:
        warn(f"Marking: {len(_unfilled)} blueprint question(s) skipped by AI: {_unfilled}")
    _unmatched: list[str] = []
    for key, grp in fill_groups.items():
        excess = len(grp) - fill_group_idx.get(key, 0)
        for fq in grp[fill_group_idx.get(key, 0):fill_group_idx.get(key, 0) + max(0, excess)]:
            _unmatched.append(fq.get("number") or str(key))
    if _unmatched:
        warn(f"Marking: AI returned question(s) with no blueprint match: {_unmatched}")
    _fix_mc_marks(result)
    for bq in result.get("questions", []):
        if not (bq.get("student_answer") or "").strip() and bq.get("assigned_marks") in (None, 0):
            bq["explanation"] = "Blank answer."
    for bq in result.get("questions", []):
        max_m = bq.get("max_marks")
        if max_m is None:
            continue
        m = bq.get("assigned_marks", 0)
        if not isinstance(m, int) or m < 0 or m > int(max_m):
            warn(
                f"Marking: Q{bq.get('number')} assigned_marks={m} out of range "
                f"[0, {max_m}] — clamping"
            )
            try:
                m_int = int(m)
            except (TypeError, ValueError):
                m_int = 0
            bq["assigned_marks"] = max(0, min(m_int, int(max_m)))
    return result


def _fix_mc_marks(result: dict) -> None:
    """Normalise student_answer and recompute assigned_marks for MCQ questions in-place.

    The AI is not shown the correct answer for MCQs, so it cannot award marks
    reliably. This function overrides assigned_marks deterministically and
    normalises the extracted letter (e.g. "b." → "B").

    Keyed by question_text (not number) because duplicate question numbers
    (e.g. two Q38s on the same page) share the same stripped number after
    _2 is removed from blueprints.
    """
    mc_correct: dict[str, str] = {
        (q.get("question_text") or "").strip(): str(q.get("correct_answer") or "").strip().upper()
        for q in result.get("questions", [])
        if q.get("question_type") == "multiple_choice" and q.get("correct_answer")
    }
    if not mc_correct:
        return
    for q in result.get("questions", []):
        qt = (q.get("question_text") or "").strip()
        if qt not in mc_correct:
            continue
        raw_ans = (q.get("student_answer") or "").strip()
        student_ans = raw_ans[0].upper() if raw_ans and raw_ans[0].isalpha() else "?"
        q["student_answer"] = student_ans
        max_m = int(q.get("max_marks") or 1)
        correct = student_ans == mc_correct[qt]
        q["assigned_marks"] = max_m if correct else 0
        q["explanation"] = "Correct." if correct else "Incorrect."
