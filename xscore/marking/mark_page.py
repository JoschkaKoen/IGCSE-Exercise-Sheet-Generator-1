"""Per-page rendering and AI marking call for the grading pipeline."""

from __future__ import annotations

import base64
import hashlib
import re
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Any

from eXercise.ai_client import collect_streamed_response
from eXercise.api_retry import retry_api_call
from xscore.config import MARKING_JPEG_QUALITY
from xscore.marking.formats.base import FormatParseError, MarkingFailure, MarkingFormat
from xscore.prompts.loader import load_prompt
from xscore.shared.prompt_logger import (
    save_input_data, save_prompt, save_response,
)
from xscore.shared.terminal_ui import info_line, warn_line


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


# Match `student_answer:` only when it sits at the question-dict indent — exactly
# 2 spaces in the YAML emitted by build_blueprint / patch_blueprint_with_answers
# (sibling to `type:`, `correct_answer:`, etc.). Block-scalar content lives at
# 4+ spaces, so this regex won't rename text that happens to mention
# `student_answer:` inside a question_text.
_STUDENT_ANSWER_LINE = re.compile(r'(?m)^(  )student_answer:')


def _rename_blueprint_for_prompt(blueprint_str: str) -> str:
    """Rename ``student_answer:`` → ``transcribed_answer:`` in the YAML blueprint
    string passed into the marking prompt.

    The pipeline stores the field as ``student_answer`` everywhere (matching
    step 28's output and the downstream report format); the rename only
    happens on the prompt-bound copy so the marking AI sees a different field
    name from the marking fields it owns. ``parse_response`` accepts either
    name when reading the AI's reply, so the round-trip is transparent.
    """
    return _STUDENT_ANSWER_LINE.sub(r'\1transcribed_answer:', blueprint_str)


def _build_marking_system_prompt(
    blueprint: dict,
    scheme_graphics: "list[tuple[str, int, str, str]]" = (),
    *,
    has_continuation: bool = False,
    fmt: "MarkingFormat | None" = None,
    is_cs: bool = False,
    has_student_answers: bool = False,
) -> str:
    """Build the system prompt shared by the JPEG and Gemini PDF marking paths.

    Step 28 (``extract_student_answers``) always runs before step 29 in the
    live pipeline; the blueprint reaches this function with student answers
    already transcribed and renamed to ``transcribed_answer``. The
    FIELD_RULES fragment instructs the marker to treat that field as
    read-only input and emit only ``assigned_marks``, ``explanation``,
    ``confidence``, and ``problem``.

    *has_student_answers* — accepted for backward compat with callers that
    haven't been updated yet; ignored.
    """
    if fmt is None:
        fmt = MarkingFormat()
    layout = blueprint.get("layout") or {"rows": 1, "cols": 1}
    rows, cols = int(layout.get("rows", 1)), int(layout.get("cols", 1))

    # --- Sections A + B + C + D: role/task, field rules, output format, format validity ---
    # The per-format ai_marking_<fmt>.md SYSTEM section embeds A, C, D around a
    # $field_rules placeholder. ai_marking_fragments.md FIELD_RULES is loaded
    # first with $criterion_ref so the assembled system prompt is byte-
    # identical to the pre-consolidation 4-method append.
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
        for _qn, _, _, _ in scheme_graphics:
            _seen[_qn] = _seen.get(_qn, 0) + 1
        _idx: dict[str, int] = {}
        _lines: list[str] = []
        for _qn, _, _, _transcript in scheme_graphics:
            _idx[_qn] = _idx.get(_qn, 0) + 1
            _label = f"image {_idx[_qn]}" if _seen[_qn] > 1 else "image"
            _hdr = f"  • Question {_qn} expected answer → {_label}"
            _t = (_transcript or "").strip()
            if _t:
                _indented = "\n".join(f"      {ln}" for ln in _t.splitlines())
                _lines.append(f"{_hdr}\n    Transcription:\n{_indented}")
            else:
                _lines.append(_hdr)
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
    scheme_graphics: list[tuple[str, int, str, str]] = (),
    fmt: "MarkingFormat | None" = None,
    extra_b64: list[str] = (),
    reuse_cache: bool = False,
    is_cs: bool = False,
    has_student_answers: bool = False,
) -> dict:
    """Vision call to fill in a marking blueprint for one scan page.

    Raises :class:`MarkingFailure` if all attempts are exhausted.
    *extra_b64* — additional continuation-page images appended after the main image.
    *reuse_cache* — when True, look up the request in
    :mod:`xscore.shared.response_cache` before calling the API; on miss, store
    the API response after a successful parse. Default False (no cache).
    *has_student_answers* — kept for backward compat; ignored. The marking
    AI always treats `transcribed_answer` as read-only input from step 28.
    """
    if fmt is None:
        fmt = MarkingFormat()
    use_stream = use_stream and fmt.prefer_stream()
    system_prompt = _build_marking_system_prompt(
        blueprint, scheme_graphics, has_continuation=bool(extra_b64), fmt=fmt, is_cs=is_cs,
        has_student_answers=has_student_answers,
    )

    _, user_text = load_prompt(
        fmt.prompt_name(), section="user",
        blueprint=_rename_blueprint_for_prompt(blueprint_xml),
    )
    # Image(s) first, text after — system → page image → continuation pages → mark-scheme graphics → user-text per audit item [5].
    _user_content: list[dict] = [
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    for _cb64 in extra_b64:
        _user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_cb64}"}})
    for _qn, _ms_page, _g_b64, _ in scheme_graphics:
        _user_content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_g_b64}"}})
    _user_content.append({"type": "text", "text": user_text})
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
    save_input_data(prompt_save_path, blueprint_xml, ext="yaml")

    # Optional response cache: only consulted when the user opted in via the
    # NL prompt ("reuse cache"). Key folds in every input that affects the
    # response so a stale hit is impossible by construction.
    _cache_key: str | None = None
    cache_hit = False
    raw_from_cache: str | None = None
    if reuse_cache:
        from xscore.shared.response_cache import cache_key as _cache_key_fn, cache_get
        # Pre-hash each base64 image to a 64-char digest before joining. The
        # raw strings are multi-MB each; folding them whole into cache_key()'s
        # SHA-256 update would re-hash megabytes per lookup. Mirrors what the
        # `image_bytes=` path does for the primary page image below.
        def _b64_digest(s: str) -> str:
            return hashlib.sha256(s.encode("ascii", errors="ignore")).hexdigest()
        _extra_hashes = (
            ",".join(_b64_digest(b) for b in extra_b64)
            + "|"
            + ",".join(_b64_digest(g[2]) for g in scheme_graphics)
        )
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
                cache_hit = True
                raw_from_cache = _cached_raw

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

    # --- First call: cache hit or live API call --------------------------
    _last_raw: str = ""
    try:
        if cache_hit and raw_from_cache is not None:
            raw, thinking_text = raw_from_cache, ""
        else:
            raw, thinking_text = retry_api_call(_do_call, label=f"Marking ({model_id})")
            save_response(prompt_save_path, raw, thinking=thinking_text)
        _last_raw = raw

        try:
            result, unfilled, unmatched = _apply_marking_response(raw, blueprint, fmt)
        except FormatParseError:
            if cache_hit:
                # Stale cache shape — discard and fall through to a live call.
                cache_hit = False
                raw, thinking_text = retry_api_call(_do_call, label=f"Marking ({model_id})")
                save_response(prompt_save_path, raw, thinking=thinking_text)
                _last_raw = raw
                result, unfilled, unmatched = _apply_marking_response(raw, blueprint, fmt)
            else:
                raise

        if unmatched:
            warn(f"Marking: AI returned question(s) with no blueprint match: {unmatched}")

        # --- Completeness retry: one shot to recover skipped questions ----
        if unfilled:
            _page_num = int(blueprint.get("page") or 0)
            _layout = blueprint.get("layout") or {"rows": 1, "cols": 1}
            unfilled_set = set(unfilled)
            slim_questions = [
                q for q in result.get("questions", [])
                if q.get("number") in unfilled_set
            ]
            info_line(
                f"Marking p{_page_num} retry: {len(unfilled)} missing question(s) — "
                + ", ".join(f"q{n}" for n in unfilled)
            )
            retry_raw, _retry_thinking = _do_retry_call(
                client, model_id, kwargs, fmt, slim_questions, unfilled,
                _page_num, _layout, prompt_save_path, use_stream,
            )
            still_unfilled: list[str] = list(unfilled)
            if retry_raw:
                slim_bp = {
                    "page": _page_num,
                    "layout": _layout,
                    "questions": slim_questions,
                }
                try:
                    _, still_unfilled, retry_unmatched = _apply_marking_response(
                        retry_raw, slim_bp, fmt,
                    )
                except FormatParseError as exc:
                    warn(f"Marking p{_page_num} retry parse error: {exc} — keeping first-call result")
                    retry_unmatched = []
                if retry_unmatched:
                    warn(
                        f"Marking: AI returned question(s) with no blueprint match (retry): "
                        f"{retry_unmatched}"
                    )
            if still_unfilled:
                warn(
                    f"Marking: {len(still_unfilled)} blueprint question(s) skipped by AI "
                    f"(after retry): {still_unfilled}"
                )

        # --- Final validation pass: MCQ fix + blank-answer + clamp --------
        _finalize_marking(result, warn)

        # The canonical marked YAML is written by run_ai_marking() under
        # 29_ai_marking/students/<S>/page_N.yaml; the prompt-logger sidecar
        # would only duplicate the same content with student_name='', so skip it.

        if reuse_cache and not cache_hit and _cache_key is not None:
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
) -> tuple[dict, list[str], list[str]]:
    """Parse a raw marking response and apply it to *blueprint*.

    Pure-ish: parses *raw*, walks *blueprint*, fills entries by ``_bq_key``
    positional match, returns ``(result, unfilled, unmatched)``. Does NOT warn,
    does NOT MCQ-fix, does NOT clamp — those are caller responsibilities so
    that retry logic can run before final validation. Raises
    :class:`FormatParseError` if *raw* is unparseable.

    Idempotent on repeated calls against partially-filled blueprints — the
    inner walk only fills bp entries whose ``assigned_marks is None``, so a
    second invocation against a slim blueprint of previously-unfilled entries
    is a clean retry-merge.
    """
    parsed_questions = fmt.parse_response(raw)
    result = blueprint.copy()
    fill_groups: dict[tuple, list] = defaultdict(list)
    for q in parsed_questions:
        fill_groups[_bq_key(q)].append(q)

    fill_group_idx: dict[tuple, int] = defaultdict(int)
    unfilled: list[str] = []
    for bq in result.get("questions", []):
        key = _bq_key(bq)
        idx = fill_group_idx[key]
        fill_group_idx[key] += 1
        # Skip bp entries that were already filled by an earlier pass — only
        # fill ones still pending. Lets the same function run idempotently as
        # a retry-merge against a slim blueprint of just-unfilled entries.
        if bq.get("assigned_marks") is not None:
            continue
        group = fill_groups.get(key, [])
        if idx < len(group):
            fq = group[idx]
            if fq.get("assigned_marks") is None:
                # AI emitted this slot but with an unparseable / empty mark.
                # Treat as if the AI hadn't emitted it: leave bq unfilled so
                # the completeness retry re-asks. The fq slot is consumed
                # (idx already advanced) — intentional, otherwise a later bp
                # entry sharing the same key would be paired with the wrong
                # fq on the positional walk.
                unfilled.append(bq.get("number"))
                continue
            # Guarded: pre-fill from step 26 (extract_student_answers) takes
            # precedence over the AI's re-emission in the marking response.
            # In presupplied mode the AI is told NOT to emit student_answer at
            # all — fall back to "" so the missing key doesn't crash the merge.
            if not bq.get("student_answer"):
                bq["student_answer"] = fq.get("student_answer", "")
            bq["assigned_marks"] = fq['assigned_marks']
            bq["explanation"] = fq['explanation']
            # Side-channel signals — copied from the AI response when
            # present. Read only by step 34's confidence audit.
            if "confidence" in fq:
                bq["confidence"] = fq["confidence"]
            if "problem" in fq:
                bq["problem"] = fq["problem"]
        else:
            unfilled.append(bq.get("number"))

    unmatched: list[str] = []
    for key, grp in fill_groups.items():
        excess = len(grp) - fill_group_idx.get(key, 0)
        for fq in grp[fill_group_idx.get(key, 0):fill_group_idx.get(key, 0) + max(0, excess)]:
            unmatched.append(fq.get("number") or str(key))

    return result, unfilled, unmatched


def _finalize_marking(result: dict, warn: Callable[[str], None]) -> None:
    """Run the final validation pass on a fully-merged marking result.

    Steps: MCQ deterministic recompute, blank-answer default text, unmarked-
    question surfacing, range clamp on ``assigned_marks``. Mutates *result* in
    place. Fires a warn for unmarked questions (AI failed to produce a mark
    after the completeness retry) and for out-of-range marks.
    """
    _fix_mc_marks(result)
    for bq in result.get("questions", []):
        if not (bq.get("student_answer") or "").strip() and bq.get("assigned_marks") in (None, 0):
            bq["explanation"] = "Blank answer."
    for bq in result.get("questions", []):
        max_m = bq.get("max_marks")
        if max_m is None:
            continue
        m = bq.get("assigned_marks")
        if m is None:
            # AI never produced a mark for this question (and the completeness
            # retry didn't recover it). Default to 0 so totals are computable,
            # but tag the explanation so per-question reports flag it for
            # manual review rather than presenting a silent 0/max grade.
            warn(
                f"Marking: Q{bq.get('number')} unmarked after retry — "
                f"defaulted to 0 (manual review required)"
            )
            bq["assigned_marks"] = 0
            if (bq.get("student_answer") or "").strip():
                bq["explanation"] = (
                    "AI marking failed — defaulted to 0; manual review required."
                )
            # else: leave the "Blank answer." explanation set in the first loop
            continue
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


def _do_retry_call(
    client: Any,
    model_id: str,
    base_kwargs: dict,
    fmt: "MarkingFormat",
    slim_questions: list[dict],
    unfilled_qnums: list[str],
    page_num: int,
    layout: dict,
    prompt_save_path: Path | None,
    use_stream: bool,
) -> tuple[str, str]:
    """Single follow-up call that re-asks the marking AI for the missing
    questions, with a slim blueprint scoped to just those entries.

    Returns ``(raw, thinking)``. On any exception (parse error, API failure,
    empty response), returns ``("", "")`` — never propagates, so the caller
    can fall back to the original warn-and-clamp path. No ``retry_api_call``
    wrapper here: a single shot is intentional, otherwise transient retries
    would compound on top of the completeness retry.
    """
    try:
        slim_xml = fmt.build_blueprint(page_num, layout, slim_questions)
        _, base_user_text = load_prompt(
            fmt.prompt_name(), section="user",
            blueprint=_rename_blueprint_for_prompt(slim_xml),
        )
        # Display labels with a leading "q" so they read naturally in the
        # emphasis line ("missing q4c, q10" rather than "missing 4c, 10").
        _qnum_str = ", ".join(f"q{n}" for n in unfilled_qnums)
        emphasis = (
            f"RETRY: your previous response was missing entries for these "
            f"question(s): {_qnum_str}. Mark each one explicitly — return one "
            "entry per number listed in the blueprint below.\n\n"
        )
        retry_user_text = emphasis + base_user_text

        # Reuse the system message and image_url parts from the first call;
        # only swap the user-prompt text.
        first_user_content = base_kwargs["messages"][1]["content"]
        if isinstance(first_user_content, list):
            image_parts = [c for c in first_user_content if c.get("type") == "image_url"]
        else:
            image_parts = []
        retry_kwargs = dict(base_kwargs)
        # Image-first ordering matches the primary call (audit item [5]).
        retry_kwargs["messages"] = [
            base_kwargs["messages"][0],
            {
                "role": "user",
                "content": [
                    *image_parts,
                    {"type": "text", "text": retry_user_text},
                ],
            },
        ]

        if prompt_save_path is not None:
            retry_prompt_path = prompt_save_path.with_name(
                prompt_save_path.stem + "_retry" + prompt_save_path.suffix
            )
            save_prompt(
                retry_prompt_path, model=model_id, messages=retry_kwargs["messages"],
            )
        else:
            retry_prompt_path = None

        if use_stream:
            _th: list[str] = []
            _stream = client.chat.completions.create(**retry_kwargs, stream=True)
            raw = collect_streamed_response(_stream, thinking_out=_th)
            thinking_text = "".join(_th)
        else:
            _resp = client.chat.completions.create(**retry_kwargs)
            raw = _resp.choices[0].message.content or ""
            thinking_text = (
                getattr(_resp.choices[0].message, "reasoning_content", "") or ""
            )

        if retry_prompt_path is not None:
            save_response(retry_prompt_path, raw, thinking=thinking_text)

        return raw, thinking_text
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Marking p{page_num} retry failed: {exc} — keeping first-call result")
        return "", ""


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
