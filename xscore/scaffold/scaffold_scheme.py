"""Step 24 — Parse the mark scheme into per-question criteria, group by group.

Pages that share a question form a connected component (computed from step 23's
``questions_per_page`` mapping). Each component is sent as ONE API call carrying
a single combined PDF of the component's pages — so a multi-page answer
(e.g. a 15-mark long-answer split across two scheme pages) reaches the model
intact and produces one coherent entry instead of two contradictory halves.

Four-way provider routing: Gemini inline PDFs, Kimi server-extracted text
(injected as a system message), Qwen ``qwen-doc-turbo`` / ``qwen-long`` via
DashScope ``fileid://`` (native PDF), other OpenAI-compatible clients (Grok,
``qwen3-vl-plus``, …) rasterized PNGs. When step 23's mapping is missing,
falls back to one singleton group per page using the full scaffold.
Attaches step 22's graphics positions onto matching scheme entries.
"""

from __future__ import annotations

import base64 as _base64
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google.genai import types as gai_types

from eXercise.ai_client import (
    build_completion_kwargs, collect_streamed_response,
    gemini_pdf_part, kimi_pdf_text, make_ai_client,
    split_gemini_response,
)
from eXercise.qwen_input import (
    model_supports_pdf_input, qwen_pdf_system_message, upload_pdf_for_extract,
)
from eXercise.api_retry import retry_api_call
from xscore.scaffold.scaffold_api import _finish_reason, _make_gen_config
from xscore.scaffold.scaffold_pages import _group_pages_by_shared_question
from xscore.scaffold.scaffold_pdf_split import combine_pdf_pages
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
from xscore.shared.prompt_logger import (
    save_input_data, save_output_data, save_prompt, save_response,
)
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
    should_cache: bool = False,
) -> dict:
    """Parse the mark scheme via Gemini (or OpenAI-compatible client) page by page.

    Reads per-page PDFs from step 22's pages dir; falls back to splitting the
    PDF if that dir doesn't exist (allows running step 24 in isolation).
    Uses *questions_per_page* (from step 23) to send only the relevant
    question entries to the AI per page; falls back to the full scaffold for
    any page missing from the mapping. Empty mapping for a page → no API call.
    Attaches ``graphics_by_qnum`` (from step 22) onto matching scheme entries.
    """
    if fmt is None:
        from xscore.scaffold.formats.base import ScaffoldFormat
        fmt = ScaffoldFormat()

    n_pages, _page_paths, _tmp_dir = _ensure_scheme_pages(marking_scheme_pdf, artifact_dir)

    # Group pages that share a question into one API call so multi-page answers
    # (e.g. a 15-mark long-answer split across two scheme pages) arrive
    # together. Step-23's per-page mapping drives the grouping; when missing,
    # fall back to one singleton group per page using the full scaffold.
    groups: list[tuple[list[int], "list[str] | None"]]
    if questions_per_page is not None:
        groups = [
            (pages, qnums)
            for pages, qnums in _group_pages_by_shared_question(questions_per_page)
        ]
        listed = {p for pages, _qnums in groups for p in pages}
        for pn in range(1, n_pages + 1):
            if pn not in listed:
                groups.append(([pn], []))  # not in mapping → empty qnums → skipped
        groups.sort(key=lambda g: g[0][0])
    else:
        # Fallback: one singleton per page; qnums=None signals "use full scaffold".
        groups = [([pn], None) for pn in range(1, n_pages + 1)]

    # Lazy fallback scaffold — built only when a group falls through to the
    # full-scaffold path (questions_per_page is None).
    _full_scaffold_str: str | None = None

    def _scaffold_for_qnums(qnums: "list[str] | None") -> tuple[str, bool]:
        """Return ``(scaffold_str, is_filtered)``. ``qnums == []`` ⇒ skip the
        call; ``qnums is None`` ⇒ use the full scaffold."""
        nonlocal _full_scaffold_str
        if qnums is None:
            if _full_scaffold_str is None:
                _full_scaffold_str = fmt.build_scheme_scaffold(raw_questions)
            return _full_scaffold_str, False
        if not qnums:
            return "", True
        filtered = _filter_questions_by_qnums(raw_questions, set(qnums))
        return fmt.build_scheme_scaffold(filtered), True

    # OpenAI-compatible client path. Kimi extracts PDFs to text (no PNG needed);
    # qwen-doc-turbo / qwen-long take native PDFs via fileid://; everything
    # else (Grok, qwen3-vl-plus, …) needs rasterized PNGs per page.
    _oa_client = None
    _oa_provider = ""
    _oa_use_stream = False
    _oa_thinking_kw: dict = {}
    _use_qwen_pdf = False
    page_pngs: dict[int, bytes] = {}
    if not scheme_model.startswith("gemini"):
        _oa_result = make_ai_client(
            model_env="READ_MARK_SCHEME_MODEL", should_cache=should_cache,
        )
        if _oa_result is None:
            raise RuntimeError(f"No API key set for mark scheme model {scheme_model!r}")
        _oa_client, _, _oa_provider, _, _ = _oa_result
        _oa_use_stream, _oa_thinking_kw = build_completion_kwargs(
            _oa_provider, scheme_thinking, scheme_max_tokens
        )
        _use_qwen_pdf = _oa_provider == "qwen" and model_supports_pdf_input(scheme_model)
        if _oa_provider != "kimi" and not _use_qwen_pdf:
            page_pngs = _rasterize_scheme_pages(marking_scheme_pdf, n_pages)

    def _call_group(pages: list[int], qnums: "list[str] | None") -> dict:
        page_label_slug = "_".join(str(p) for p in pages)
        scaffold_str, _is_filtered = _scaffold_for_qnums(qnums)
        if _is_filtered and not scaffold_str:
            ok_line(f"p{page_label_slug}  ·  no questions assigned — skipped")
            return {"questions": []}
        if _oa_client is None:
            _input_label = "PDF"
        elif _oa_provider == "kimi":
            _input_label = "PDF"  # Kimi sees server-extracted PDF text
        elif _use_qwen_pdf:
            _input_label = "PDF"  # Qwen sees native PDF via fileid://
        else:
            _input_label = "image"
        user_msg = fmt.build_scheme_user_msg(
            scaffold_str, pages, n_pages, input_label=_input_label,
        )
        resp_for_finish: object | None = None

        # Materialise the group's pages as one combined PDF so the provider
        # helpers (gemini_pdf_part, kimi_pdf_text, upload_pdf_for_extract)
        # can take a single Path. PNG path attaches per-page PNGs from
        # page_pngs directly and doesn't need the combined file.
        from xscore.shared.prompt_logger import attachment_part
        import tempfile
        combined_bytes: bytes | None = None
        combined_path: Path | None = None
        need_combined_pdf = (
            _oa_client is None             # Gemini native path
            or _oa_provider == "kimi"
            or _use_qwen_pdf
        )
        if need_combined_pdf:
            combined_bytes = combine_pdf_pages(marking_scheme_pdf, pages)
            _tmp = tempfile.NamedTemporaryFile(
                prefix=f"scheme_p{page_label_slug}_",
                suffix=".pdf", delete=False,
            )
            try:
                _tmp.write(combined_bytes)
                _tmp.flush()
            finally:
                _tmp.close()
            combined_path = Path(_tmp.name)

        try:
            # Hoist messages construction so it feeds both the API call and the
            # audit log. For the Gemini path build a parallel OpenAI-shape audit
            # list mirroring what the native Part-based call sends.
            _messages: list = []
            if _oa_client is None:
                _audit_messages: list = [
                    {"role": "system", "content": fmt.system_scheme_prompt(is_cs=is_cs)},
                    {"role": "user", "content": [
                        attachment_part(combined_bytes, "application/pdf"),
                        {"type": "text", "text": user_msg},
                    ]},
                ]
            else:
                if _oa_provider == "kimi":
                    _page_text = kimi_pdf_text(
                        _oa_client, combined_path,
                        label=f"scheme p{page_label_slug}",
                    )
                    _messages = [
                        {"role": "system", "content": fmt.system_scheme_prompt(is_cs=is_cs)},
                        {"role": "system", "content": _page_text},
                        {"role": "user", "content": user_msg},
                    ]
                elif _use_qwen_pdf:
                    file_id = upload_pdf_for_extract(_oa_client, combined_path)
                    _messages = [
                        {"role": "system", "content": fmt.system_scheme_prompt(is_cs=is_cs)},
                        qwen_pdf_system_message(file_id),
                        {"role": "user", "content": user_msg},
                    ]
                else:
                    # PNG from page_pngs: 300 DPI lossless — preserves fine mark-scheme text.
                    # One image_url part per page in mark-scheme order.
                    _content: list = []
                    for _p in pages:
                        _b64 = _base64.b64encode(page_pngs[_p]).decode()
                        _content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{_b64}"},
                        })
                    _content.append({"type": "text", "text": user_msg})
                    _messages = [
                        {"role": "system", "content": fmt.system_scheme_prompt(is_cs=is_cs)},
                        {"role": "user", "content": _content},
                    ]
                _audit_messages = _messages

            def _do_call() -> tuple[str, str, object | None]:
                if _oa_client is not None:
                    # OpenAI-compatible path. Kimi extracts the combined PDF
                    # server-side to text and injects it as a system message;
                    # qwen-doc-turbo / qwen-long take the combined PDF
                    # natively via fileid://; everything else gets per-page
                    # rasterised PNGs.
                    kwargs: dict = dict(
                        model=scheme_model,
                        messages=_messages,
                    )
                    kwargs.update(_oa_thinking_kw)
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
                # Gemini native path — combined PDF inlined as one Part
                _resp = client.models.generate_content(
                    model=scheme_model,
                    contents=[
                        gemini_pdf_part(
                            client, combined_path,
                            label=f"scheme p{page_label_slug}",
                        ),
                        gai_types.Part.from_text(text=user_msg),
                    ],
                    config=_make_gen_config(
                        scheme_thinking, fmt.system_scheme_prompt(is_cs=is_cs),
                        max_tokens=scheme_max_tokens,
                    ),
                )
                _raw, _th = split_gemini_response(_resp)
                return _raw, _th, _resp

            _t0 = time.perf_counter()
            try:
                raw, thinking_text, resp_for_finish = retry_api_call(
                    _do_call, label=f"Mark scheme p{page_label_slug}",
                )
            except Exception as _exc:
                # All attempts exhausted — degrade to empty result so the rest of
                # the mark scheme can still be assembled.
                warn_line(
                    f"Mark scheme p{page_label_slug}: giving up after retries  ·  "
                    f"{format_duration(time.perf_counter() - _t0)}  —  {_exc}"
                )
                return {"questions": []}
            if not raw:
                _reason = "" if _oa_client is not None else f" ({_finish_reason(resp_for_finish)})"
                warn_line(f"Mark scheme p{page_label_slug}: empty response{_reason}")
            if artifact_dir is not None:
                _prompt_path = artifact_scaffold_prompt_path(
                    artifact_dir, f"mark_scheme_p{page_label_slug}",
                )
                save_prompt(_prompt_path, model=scheme_model, messages=_audit_messages)
                save_input_data(_prompt_path, scaffold_str, ext="yaml")
                save_response(_prompt_path, raw or "", thinking=thinking_text)
                if raw:
                    save_output_data(_prompt_path, raw, ext="yaml")
            try:
                parsed = fmt.parse_scheme_response(raw or "")
            except RuntimeError as _exc:
                warn_line(f"Mark scheme p{page_label_slug}: parse error  —  {_exc}")
                ok_line(f"p{page_label_slug}  ·  parse error  ·  {format_duration(time.perf_counter() - _t0)}")
                return {"questions": []}
            _qnums_with_content = [
                str(_q.get("number", ""))
                for _q in parsed.get("questions", [])
                if (
                    str(_q.get("mark_scheme_answer") or "").strip()
                    or str(_q.get("explanation") or "").strip()
                    or str(_q.get("correct_answer") or "").strip()
                    or (_q.get("mark_scheme") or [])
                )
            ]
            _qs_str = (", ".join(f"q{q}" for q in _qnums_with_content)) if _qnums_with_content else "—"
            ok_line(f"p{page_label_slug}  ·  {_qs_str}  ·  {format_duration(time.perf_counter() - _t0)}")
            return parsed
        finally:
            if combined_path is not None:
                combined_path.unlink(missing_ok=True)

    _max_workers = max(1, min(len(groups), int(os.environ.get("PARSE_SCHEME_WORKERS", "500"))))
    with ThreadPoolExecutor(max_workers=_max_workers) as pool:
        page_results = list(pool.map(lambda g: _call_group(*g), groups))

    result = _merge_scheme_results(page_results)

    # Warn about leaf questions we expected the AI to extract content for but
    # got an empty response back (no correct_answer AND no criteria).
    # Scoping: when step 23 produced a per-page mapping, expect only its union;
    # otherwise expect every leaf in raw_questions (full-scaffold fallback path).
    # Always intersect with leaves — parent questions (those with subquestions)
    # structurally have no own criteria in Cambridge mark schemes, so step 23
    # listing them (it sometimes over-generalises from seeing children) is not
    # an actionable miss for step 24.
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
        if (
            str(_q.get("mark_scheme_answer") or "").strip()
            or str(_q.get("explanation") or "").strip()
            or str(_q.get("correct_answer") or "").strip()
            or (_q.get("mark_scheme") or [])
        )
    }
    _missing_content = sorted(_expected - _with_content)
    if _missing_content:
        warn_line(
            f"Mark scheme: no content extracted for {len(_missing_content)} question(s): "
            + ", ".join(f"q{q}" for q in _missing_content)
        )

    # Attach graphics positions from step 22 onto matching scheme entries.
    if graphics_by_qnum:
        for _q in result.get("questions", []):
            _key = _norm_qnum(_q["number"])
            if _key in graphics_by_qnum:
                _q["graphics"] = graphics_by_qnum[_key]

    # Save merged artifacts under step 24's folder.
    if artifact_dir is not None:
        try:
            from xscore.scaffold.scaffold_markdown import write_mark_scheme_markdown
            import yaml as _yaml
            from xscore.scaffold.formats.base import _ScaffoldDumper
            _out_str = _yaml.dump(
                result, Dumper=_ScaffoldDumper,
                allow_unicode=True, default_flow_style=False, sort_keys=False,
            )
            p = artifact_mark_scheme_path(artifact_dir, fmt="yaml")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(_out_str, encoding="utf-8")
            write_mark_scheme_markdown(artifact_dir, result.get("questions", []))
        except Exception:
            pass

    if _tmp_dir is not None:
        import shutil
        shutil.rmtree(_tmp_dir, ignore_errors=True)
    return result
