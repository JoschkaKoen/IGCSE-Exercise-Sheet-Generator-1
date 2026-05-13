"""Per-page rendering and AI marking call for the grading pipeline.

The marking step splits naturally into three concerns:

- **Prompt construction** — :mod:`xscore.marking.mark_page_prompts` builds the
  ``ai_marking.md`` (or ``ai_marking_mcq.md``) system + user prompts and
  renders the YAML blueprint for the ``$blueprint`` substitution.
- **The marking call + retry** — this module: :func:`_render_page_b64` to
  build the JPEG, :func:`_mark_page` to make the vision call with the
  parse-failure reprompt + completeness-retry envelope, and
  :func:`_do_retry_call` for the slim-blueprint follow-up.
- **Response post-processing** — :mod:`xscore.marking.mark_page_postprocess`
  parses the response, fills the blueprint, and runs the final validation
  pass (MCQ marks, blank-answer text, range clamp).

Symbols from the two sibling modules are re-exported below for backward
compatibility with historic ``from xscore.marking.mark_page import …`` call
sites (notably :mod:`xscore.marking.ai_mark`).
"""

from __future__ import annotations

import base64
import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

from eXercise.ai_client import collect_streamed_response
from eXercise.api_retry import retry_api_call
from xscore.config import MARKING_JPEG_QUALITY
from xscore.marking.extract_answers import blueprint_for_marking
from xscore.marking.formats.base import FormatParseError, MarkingFailure, MarkingFormat
from xscore.marking.mark_page_postprocess import (  # noqa: F401  (re-exported)
    _apply_marking_response,
    _finalize_marking,
    _fix_mc_marks,
    _normalize_mc_answer,
)
from xscore.marking.mark_page_prompts import (  # noqa: F401  (re-exported)
    _blueprint_for_prompt,
    _bq_key,
    _build_marking_system_prompt,
)
from xscore.prompts.loader import load_prompt
from xscore.shared.models import ExamLayout
from xscore.shared.prompt_logger import (
    save_input_data, save_prompt, save_response,
)
from xscore.shared.terminal_ui import info_line, warn_line


def _render_page_b64(doc: Any, page_idx: int, dpi: int = 300) -> str:
    """Render a fitz Document page at *page_idx* as base64 JPEG.

    Fast path: scanned_exam_merged_and_angles_adjusted.pdf (built by deskew_pdf_raster) embeds exactly one
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
    is_all_mcq: bool = False,
    request_timeout: httpx.Timeout | None = None,
) -> tuple[dict, list[dict]]:
    """Vision call to fill in a marking blueprint for one scan page.

    Returns ``(result, mcq_corrections)``. ``mcq_corrections`` is a list of
    ``{"number", "from", "to"}`` dicts collected from MCQ slots where the AI
    emitted a ``corrected_student_answer`` letter different from the
    extraction. Empty list when nothing was corrected.

    Raises :class:`MarkingFailure` if all attempts are exhausted.
    *extra_b64* — additional continuation-page images appended after the main image.
    *reuse_cache* — when True, look up the request in
    :mod:`xscore.shared.response_cache` before calling the API; on miss, store
    the API response after a successful parse. Default False (no cache).
    *has_student_answers* — kept for backward compat; ignored. The marking
    AI always treats `transcribed_answer` as read-only input from extract_student_answers.
    *is_all_mcq* — when True, swap in the short ``ai_marking_mcq`` prompt
    (no FIELD_RULES, no CONTINUATION, no CODE_FORMATTING) since MCQ marks
    are auto-computed and the AI's role is verify-only.
    *request_timeout* — per-call HTTP timeout forwarded to the OpenAI client.
    The ``read`` component fires per streaming chunk, so a stalled upstream
    raises ``httpx.ReadTimeout`` mid-iteration and triggers the standard
    retry envelope. None (default) means no override (SDK default applies).
    """
    if fmt is None:
        fmt = MarkingFormat()
    use_stream = use_stream and fmt.prefer_stream()
    system_prompt = _build_marking_system_prompt(
        blueprint, scheme_graphics, has_continuation=bool(extra_b64), fmt=fmt, is_cs=is_cs,
        has_student_answers=has_student_answers, is_all_mcq=is_all_mcq,
    )

    _user_prompt_name = "ai_marking_mcq" if is_all_mcq else fmt.prompt_name()
    _, user_text = load_prompt(
        _user_prompt_name, section="user",
        blueprint=_blueprint_for_prompt(blueprint_xml),
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

    _call_kw: dict = {"timeout": request_timeout} if request_timeout is not None else {}

    def _do_call() -> tuple[str, str]:
        if use_stream:
            # Stream consumed inside the closure so a mid-stream failure retries.
            _th: list[str] = []
            _stream = client.chat.completions.create(**kwargs, stream=True, **_call_kw)
            return collect_streamed_response(_stream, thinking_out=_th), "".join(_th)
        _resp = client.chat.completions.create(**kwargs, **_call_kw)
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
            result, unfilled, unmatched, mcq_corrections = _apply_marking_response(raw, blueprint, fmt)
        except FormatParseError as exc:
            if cache_hit:
                # Stale cache shape — discard and fall through to a live call.
                cache_hit = False
                raw, thinking_text = retry_api_call(_do_call, label=f"Marking ({model_id})")
                save_response(prompt_save_path, raw, thinking=thinking_text)
                _last_raw = raw
                result, unfilled, unmatched, mcq_corrections = _apply_marking_response(raw, blueprint, fmt)
            else:
                # Live-call parse failure (after repair + list/flat fallbacks
                # have all failed inside _apply_marking_response). One fresh
                # API call with a parse-failure emphasis prefix on the user
                # text; if that also fails to parse, raise MarkingFailure(
                # attempts=2) so the _mark_one_page handler can run the
                # extraction-only fallback (D3).
                warn(f"Marking {blueprint.get('student_name', '?')} parse error — retrying once ({exc})")
                _emphasis = (
                    "RETRY: your previous response could not be parsed as YAML. "
                    "Re-emit per the SYSTEM rules — wrap under a top-level "
                    "`questions:` key, use `''` for empty fields and `|` block "
                    "scalars for non-empty free-text fields, never use double "
                    "quotes, and keep `problem` to ONE short sentence.\n\n"
                )
                _reparse_user_content = list(kwargs["messages"][1]["content"])
                _text_idxs = [
                    _i for _i, _c in enumerate(_reparse_user_content)
                    if isinstance(_c, dict) and _c.get("type") == "text"
                ]
                if _text_idxs:
                    _idx = _text_idxs[-1]
                    _reparse_user_content[_idx] = {
                        "type": "text",
                        "text": _emphasis + _reparse_user_content[_idx].get("text", ""),
                    }
                _reparse_kwargs = dict(kwargs)
                _reparse_kwargs["messages"] = [
                    kwargs["messages"][0],
                    {"role": "user", "content": _reparse_user_content},
                ]

                def _do_call_reparse() -> tuple[str, str]:
                    if use_stream:
                        _th: list[str] = []
                        _stream = client.chat.completions.create(**_reparse_kwargs, stream=True, **_call_kw)
                        return collect_streamed_response(_stream, thinking_out=_th), "".join(_th)
                    _resp = client.chat.completions.create(**_reparse_kwargs, **_call_kw)
                    return (
                        _resp.choices[0].message.content or "",
                        getattr(_resp.choices[0].message, "reasoning_content", "") or "",
                    )

                _reparse_save_path = (
                    prompt_save_path.with_name(
                        prompt_save_path.stem + "_reparse" + prompt_save_path.suffix
                    )
                    if prompt_save_path is not None else None
                )
                if _reparse_save_path is not None:
                    save_prompt(
                        _reparse_save_path, model=model_id,
                        messages=_reparse_kwargs["messages"],
                    )
                raw, thinking_text = retry_api_call(
                    _do_call_reparse, label=f"Marking reparse ({model_id})"
                )
                save_response(_reparse_save_path, raw, thinking=thinking_text)
                _last_raw = raw
                try:
                    result, unfilled, unmatched, mcq_corrections = _apply_marking_response(
                        raw, blueprint, fmt,
                    )
                except FormatParseError as exc2:
                    raise MarkingFailure(attempts=2, last_exc=exc2, last_raw=_last_raw)

        if unmatched:
            warn(f"Marking {blueprint.get('student_name', '?')}: AI returned question(s) with no blueprint match: {unmatched}")

        # --- Completeness retry: one shot to recover skipped questions ----
        if unfilled:
            _page_num = int(blueprint.get("page") or 0)
            _layout = blueprint.get("layout") or {"rows": 1, "cols": 1}
            unfilled_set = set(unfilled)
            slim_questions = [
                q for q in result.get("questions", [])
                if q.get("number") in unfilled_set
            ]
            _student_name = blueprint.get("student_name", "?")
            info_line(
                f"Marking {_student_name} p{_page_num} retry: {len(unfilled)} missing question(s) — "
                + ", ".join(f"q{n}" for n in unfilled)
            )
            retry_raw, _retry_thinking = _do_retry_call(
                client, model_id, kwargs, fmt, slim_questions, unfilled,
                _page_num, _layout, prompt_save_path, use_stream,
                request_timeout=request_timeout,
                student_name=_student_name,
            )
            still_unfilled: list[str] = list(unfilled)
            if retry_raw:
                slim_bp = {
                    "page": _page_num,
                    "layout": _layout,
                    "questions": slim_questions,
                    "student_name": _student_name,
                }
                try:
                    _, still_unfilled, retry_unmatched, _retry_corrections = _apply_marking_response(
                        retry_raw, slim_bp, fmt,
                    )
                    # Retry's slim blueprint contains only non-MCQ unfilled
                    # questions, so _retry_corrections will be empty by
                    # construction. Discard.
                except FormatParseError as exc:
                    warn(f"Marking {_student_name} p{_page_num} retry parse error: {exc} — keeping first-call result")
                    retry_unmatched = []
                if retry_unmatched:
                    warn(
                        f"Marking {_student_name}: AI returned question(s) with no blueprint match (retry): "
                        f"{retry_unmatched}"
                    )
            if still_unfilled:
                warn(
                    f"Marking {_student_name}: {len(still_unfilled)} blueprint question(s) skipped by AI "
                    f"(after retry): {still_unfilled}"
                )

        # --- Final validation pass: MCQ fix + blank-answer + clamp --------
        _finalize_marking(result, warn)

        # The canonical marked YAML is written by run_ai_marking() under
        # 27_ai_marking/students/<S>/page_N.yaml; the prompt-logger sidecar
        # would only duplicate the same content with student_name='', so skip it.

        if reuse_cache and not cache_hit and _cache_key is not None:
            from xscore.shared.response_cache import cache_put
            cache_put(_cache_key, model=model_id, response=raw)
        return result, mcq_corrections
    except FormatParseError as exc:
        warn(f"Marking {blueprint.get('student_name', '?')} parse error — marking aborted ({exc})")
        raise MarkingFailure(attempts=1, last_exc=exc, last_raw=_last_raw)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        raise MarkingFailure(attempts=1, last_exc=exc, last_raw=_last_raw)


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
    request_timeout: httpx.Timeout | None = None,
    student_name: str = "?",
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
        # build_blueprint expects ExamLayout (attribute access on .rows/.cols).
        layout_obj = ExamLayout(rows=int(layout.get("rows", 1)), cols=int(layout.get("cols", 1)))
        slim_xml = fmt.build_blueprint(page_num, layout_obj, slim_questions)
        # Strip the answer key before the AI sees the blueprint — see
        # blueprint_for_marking docstring for the failure mode this prevents.
        slim_xml = blueprint_for_marking(slim_xml)
        _, base_user_text = load_prompt(
            fmt.prompt_name(), section="user",
            blueprint=_blueprint_for_prompt(slim_xml),
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

        _call_kw: dict = {"timeout": request_timeout} if request_timeout is not None else {}
        if use_stream:
            _th: list[str] = []
            _stream = client.chat.completions.create(**retry_kwargs, stream=True, **_call_kw)
            raw = collect_streamed_response(_stream, thinking_out=_th)
            thinking_text = "".join(_th)
        else:
            _resp = client.chat.completions.create(**retry_kwargs, **_call_kw)
            raw = _resp.choices[0].message.content or ""
            thinking_text = (
                getattr(_resp.choices[0].message, "reasoning_content", "") or ""
            )

        if retry_prompt_path is not None:
            save_response(retry_prompt_path, raw, thinking=thinking_text)

        return raw, thinking_text
    except KeyboardInterrupt:
        raise
    except (AttributeError, TypeError, NameError, ImportError, KeyError):
        # Programming bugs are deterministic — re-raise so they crash loudly
        # instead of silently degrading every retry attempt forever.
        raise
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Marking {student_name} p{page_num} retry failed: {exc} — keeping first-call result")
        return "", ""
