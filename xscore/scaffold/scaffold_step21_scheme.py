"""Step 21 — Parse the mark scheme into per-question criteria, page by page.

Dual-provider routing (Gemini inline PDFs vs OpenAI-compatible PNGs). Uses
step 20's per-page question mapping to filter the scaffold sent per page;
falls back to the full scaffold for any page missing from the mapping.
Attaches step 19's graphics positions onto matching scheme entries.
"""

from __future__ import annotations

import base64 as _base64
import json
import os
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google.genai import types as gai_types

from eXercise.ai_client import (
    build_completion_kwargs, collect_streamed_response,
    gemini_pdf_part, make_ai_client, make_gemini_native_client,
    split_gemini_response,
)
from eXercise.api_retry import retry_api_call
from xscore.scaffold.scaffold_api import _finish_reason, _make_gen_config
from xscore.scaffold.scaffold_qtree import (
    _filter_questions_by_qnums, _leaf_qnums, _norm_qnum,
)
from xscore.scaffold.scaffold_scheme_pdf import (
    _ensure_scheme_pages, _rasterize_scheme_pages,
)
from xscore.scaffold.scaffold_xml import _merge_scheme_results
from xscore.shared.exam_paths import (
    artifact_mark_scheme_path, artifact_scaffold_prompt_path,
)
from xscore.shared.prompt_logger import save_prompt, save_response
from xscore.shared.terminal_ui import format_duration, ok_line, warn_line


def parse_mark_scheme_pages(
    client,
    scheme_model: str,
    scheme_thinking: int | None,
    scheme_max_tokens: int | None,
    *,
    marking_scheme_pdf: Path,
    raw_questions: list[dict],
    questions_per_page: "dict[int, list[str]] | None" = None,
    graphics_by_qnum: "dict[str, list] | None" = None,
    artifact_dir: "Path | None",
    fmt=None,
    is_cs: bool = False,
) -> dict:
    """Parse the mark scheme via Gemini (or OpenAI-compatible client) page by page.

    Reads per-page PDFs from step 19's pages dir; falls back to splitting the
    PDF if that dir doesn't exist (allows running step 21 in isolation).
    Uses *questions_per_page* (from step 20) to send only the relevant
    question entries to the AI per page; falls back to the full scaffold for
    any page missing from the mapping. Empty mapping for a page → no API call.
    Attaches ``graphics_by_qnum`` (from step 19) onto matching scheme entries.
    """
    if fmt is None:
        from xscore.scaffold.formats.xml_format import XmlScaffoldFormat
        fmt = XmlScaffoldFormat()

    n_pages, page_paths, _tmp_dir = _ensure_scheme_pages(marking_scheme_pdf, artifact_dir)

    # Lazy fallback scaffold — built only when a page has no per-page mapping.
    _full_scaffold_str: str | None = None

    def _scaffold_for_page(page_num: int) -> tuple[str, bool]:
        """Return ``(scaffold_str, is_filtered)`` for *page_num*. Empty filtered
        list signals "no questions on this page" — caller skips the API call."""
        nonlocal _full_scaffold_str
        if questions_per_page is not None and page_num in questions_per_page:
            qnums = questions_per_page[page_num]
            if not qnums:
                return "", True  # signal "skip"
            filtered = _filter_questions_by_qnums(raw_questions, set(qnums))
            return fmt.build_scheme_scaffold(filtered), True
        if _full_scaffold_str is None:
            _full_scaffold_str = fmt.build_scheme_scaffold(raw_questions)
        return _full_scaffold_str, False

    # OpenAI-compatible client path requires PNGs — only rasterize when needed.
    _oa_client = None
    _oa_use_stream = False
    _oa_thinking_kw: dict = {}
    page_pngs: dict[int, bytes] = {}
    if not scheme_model.startswith("gemini"):
        _oa_result = make_ai_client(model_env="READ_MARK_SCHEME_MODEL")
        if _oa_result is None:
            raise RuntimeError(f"No API key set for mark scheme model {scheme_model!r}")
        _oa_client, _, _oa_provider, _, _ = _oa_result
        _oa_use_stream, _oa_thinking_kw = build_completion_kwargs(
            _oa_provider, scheme_thinking, scheme_max_tokens
        )
        page_pngs = _rasterize_scheme_pages(marking_scheme_pdf, n_pages)

    # Gemini path inlines per-page PDFs via gemini_pdf_part inside the worker
    # (no upload pool needed). Build a quick lookup of page_num → Path.
    page_path_by_num: dict[int, Path] = {pn: p for pn, p in enumerate(page_paths, 1)}

    def _call_page(page_num: int) -> dict:
        scaffold_str, _is_filtered = _scaffold_for_page(page_num)
        if _is_filtered and not scaffold_str:
            ok_line(f"p{page_num}  ·  no questions assigned — skipped")
            return {"questions": []}
        _input_label = "image" if _oa_client is not None else "PDF"
        user_msg = fmt.build_scheme_user_msg(scaffold_str, page_num, n_pages, input_label=_input_label)
        resp_for_finish: object | None = None
        # Per-worker Gemini client so each thread has its own httpx pool — avoids
        # stale keepalive sockets carried over from earlier Gemini steps.
        page_client = client
        if _oa_client is None:
            page_client = make_gemini_native_client(deterministic=True) or client

        def _do_call() -> tuple[str, str, object | None]:
            if _oa_client is not None:
                # OpenAI-compatible path (Qwen, Grok, etc.)
                # PNG from page_pngs: 300 DPI lossless — preserves fine mark-scheme text
                b64 = _base64.b64encode(page_pngs[page_num]).decode()
                kwargs: dict = dict(
                    model=scheme_model,
                    messages=[
                        {"role": "system", "content": fmt.system_scheme_prompt(is_cs=is_cs)},
                        {"role": "user", "content": [
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/png;base64,{b64}"}},
                            {"type": "text", "text": user_msg},
                        ]},
                    ],
                )
                kwargs.update(_oa_thinking_kw)
                kwargs.update(fmt.scheme_oa_extra_kwargs(scheme_model))
                if _oa_use_stream:
                    _th: list[str] = []
                    # Stream consumed *inside* the closure so a mid-stream SSL EOF
                    # triggers a retry rather than returning a partial response.
                    stream = _oa_client.chat.completions.create(**kwargs, stream=True)
                    _raw = collect_streamed_response(stream, thinking_out=_th)
                    return _raw, "".join(_th), None
                _resp = _oa_client.chat.completions.create(**kwargs)
                return (
                    _resp.choices[0].message.content or "",
                    getattr(_resp.choices[0].message, "reasoning_content", "") or "",
                    None,
                )
            # Gemini native path — inline PDF bytes per page
            _resp = page_client.models.generate_content(
                model=scheme_model,
                contents=[
                    gemini_pdf_part(
                        page_client, page_path_by_num[page_num],
                        label=f"scheme p{page_num}",
                    ),
                    gai_types.Part.from_text(text=user_msg),
                ],
                config=_make_gen_config(
                    scheme_thinking, fmt.system_scheme_prompt(is_cs=is_cs),
                    pydantic_schema=fmt.pydantic_schema_scheme(),
                    max_tokens=scheme_max_tokens,
                ),
            )
            _raw, _th = split_gemini_response(_resp)
            return _raw, _th, _resp

        _t0 = time.perf_counter()
        try:
            raw, thinking_text, resp_for_finish = retry_api_call(
                _do_call, label=f"Mark scheme p{page_num}",
            )
        except Exception as _exc:
            # All attempts exhausted — degrade to empty page so the rest of the
            # mark scheme can still be assembled.
            warn_line(
                f"Mark scheme p{page_num}: giving up after retries  ·  "
                f"{format_duration(time.perf_counter() - _t0)}  —  {_exc}"
            )
            return {"questions": []}
        if not raw:
            _reason = "" if _oa_client is not None else f" ({_finish_reason(resp_for_finish)})"
            warn_line(f"Mark scheme p{page_num}: empty response{_reason}")
        if artifact_dir is not None:
            _prompt_path = artifact_scaffold_prompt_path(artifact_dir, f"mark_scheme_p{page_num}")
            save_prompt(
                _prompt_path,
                model=scheme_model, system=fmt.system_scheme_prompt(is_cs=is_cs),
                messages=[{
                    "role": "user",
                    "content": f"[PDF: {marking_scheme_pdf.name} p{page_num}]\n\n{user_msg}",
                }],
            )
            save_response(_prompt_path, raw or "", thinking=thinking_text)
        try:
            parsed = fmt.parse_scheme_response(raw or "")
        except RuntimeError as _exc:
            warn_line(f"Mark scheme p{page_num}: parse error  —  {_exc}")
            ok_line(f"p{page_num}  ·  parse error  ·  {format_duration(time.perf_counter() - _t0)}")
            return {"questions": []}
        _qnums_with_content = [
            str(_q.get("number", ""))
            for _q in parsed.get("questions", [])
            if (_q.get("correct_answer") or "").strip() or (_q.get("mark_scheme") or [])
        ]
        _qs_str = (", ".join(f"q{q}" for q in _qnums_with_content)) if _qnums_with_content else "—"
        ok_line(f"p{page_num}  ·  {_qs_str}  ·  {format_duration(time.perf_counter() - _t0)}")
        return parsed

    with ThreadPoolExecutor(max_workers=min(n_pages, int(os.environ.get("PARSE_SCHEME_WORKERS", "500")))) as pool:
        page_results = list(pool.map(_call_page, range(1, n_pages + 1)))

    result = _merge_scheme_results(page_results)

    # Warn about leaf questions we expected the AI to extract content for but
    # got an empty response back (no correct_answer AND no criteria).
    # Scoping: when step 20 produced a per-page mapping, expect only its union;
    # otherwise expect every leaf in raw_questions (full-scaffold fallback path).
    # Always intersect with leaves — parent questions (those with subquestions)
    # structurally have no own criteria in Cambridge mark schemes, so step 20
    # listing them (it sometimes over-generalises from seeing children) is not
    # an actionable miss for step 21.
    _leaves = set(_leaf_qnums(raw_questions))
    if questions_per_page:
        _expected: set[str] = set()
        for _qs in questions_per_page.values():
            _expected.update(_qs)
        _expected &= _leaves
    else:
        _expected = _leaves
    _with_content = {
        str(_q.get("number", "")).strip()
        for _q in result.get("questions", [])
        if (_q.get("correct_answer") or "").strip() or (_q.get("mark_scheme") or [])
    }
    _missing_content = sorted(_expected - _with_content)
    if _missing_content:
        warn_line(
            f"Mark scheme: no content extracted for {len(_missing_content)} question(s): "
            + ", ".join(f"q{q}" for q in _missing_content)
        )

    # Attach graphics positions from step 19 onto matching scheme entries.
    if graphics_by_qnum:
        for _q in result.get("questions", []):
            _key = _norm_qnum(_q["number"])
            if _key in graphics_by_qnum:
                _q["graphics"] = graphics_by_qnum[_key]

    # Save merged artifacts under step 19's folder.
    if artifact_dir is not None:
        try:
            from xscore.scaffold.scaffold_markdown import write_mark_scheme_markdown
            _ext = fmt.artifact_ext()
            if _ext == "xml":
                _root = ET.Element("scheme")
                for _q in result.get("questions", []):
                    _qel = ET.SubElement(_root, "question")
                    _qel.set("number", _q["number"])
                    _qel.set("correct_answer", _q.get("correct_answer") or "")
                    for _c in (_q.get("mark_scheme") or []):
                        _cel = ET.SubElement(_qel, "criterion")
                        _cel.set("mark", _c.get("mark", ""))
                        _cel.text = _c.get("criterion", "")
                ET.indent(_root)
                _out_str = ET.tostring(_root, encoding="unicode", xml_declaration=False)
            elif _ext == "yaml":
                import yaml as _yaml
                _out_str = _yaml.dump(result, allow_unicode=True, default_flow_style=False)
            else:
                _out_str = json.dumps(result, ensure_ascii=False, indent=2)
            p = artifact_mark_scheme_path(artifact_dir, fmt=_ext)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_out_str, encoding="utf-8")
            write_mark_scheme_markdown(artifact_dir, result.get("questions", []))
        except Exception:
            pass

    if _tmp_dir is not None:
        import shutil
        shutil.rmtree(_tmp_dir, ignore_errors=True)
    return result
