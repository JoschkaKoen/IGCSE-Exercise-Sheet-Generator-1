"""Step 20 worker — extract_exam_questions.

Per-page parallel calls that populate ``text`` and ``options`` for each
question listed on a page. Modeled on
:func:`xscore.scaffold.scaffold_scheme.parse_mark_scheme_pages`:
``ThreadPoolExecutor`` over post-cut PDF pages, four-way provider dispatch
per page, graceful empty-page degradation on retry exhaustion.

Returns the same scaffold tree step 19 produced, mutated in place to add
``text`` / ``answer_options`` on every node whose ``number`` was covered by
a successful page response.
"""

from __future__ import annotations

import base64 as _base64
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google.genai import types as gai_types

from eXercise.ai_client import (
    build_completion_kwargs, collect_streamed_response,
    gemini_pdf_part, kimi_pdf_text, make_ai_client, split_gemini_response,
)
from eXercise.api_retry import retry_api_call
from eXercise.qwen_input import (
    model_supports_pdf_input, qwen_pdf_system_message, upload_pdf_for_extract,
)
from xscore.scaffold.scaffold_api import _make_gen_config
from xscore.scaffold.scaffold_pdf_split import split_pdf_into_pages
from xscore.scaffold.scaffold_qtree import (
    _filter_questions_by_qnums, _format_qnums_for_line,
)
from xscore.shared.exam_paths import (
    artifact_exam_pages_dir, artifact_scaffold_prompt_path,
)
from xscore.shared.prompt_logger import (
    save_input_data, save_output_data, save_prompt, save_response,
)
from xscore.shared.terminal_ui import (
    format_duration, info_line, ok_line, warn_line,
)


def _expected_qnums_by_page(scaffold_nodes: list[dict]) -> dict[int, list[str]]:
    """Group every node's ``number`` by its ``page`` (parents AND leaves —
    a parent's stem text lives on its own page, so its ``number`` must be in
    that page's expected list)."""
    out: dict[int, list[str]] = {}

    def visit(node: dict) -> None:
        num = str(node.get("number", "")).strip()
        if num:
            page = max(1, int(node.get("page") or 1))
            out.setdefault(page, []).append(num)
        for sub in (node.get("subquestions") or []):
            visit(sub)

    for q in scaffold_nodes:
        visit(q)
    for page in out:
        out[page] = sorted(set(out[page]))
    return out


def _ensure_exam_pages(
    actual_exam_pdf: Path, artifact_dir: "Path | None",
) -> tuple[int, list[Path], "Path | None"]:
    """Reuse per-page splits if already on disk under the step-18 pages dir;
    otherwise create them. Returns ``(n_pages, page_paths, tmp_dir)``."""
    if artifact_dir is not None:
        pages_dir = artifact_exam_pages_dir(artifact_dir)
        if pages_dir.is_dir():
            page_paths = sorted(
                pages_dir.glob("page_*.pdf"),
                key=lambda p: int(re.search(r"page_(\d+)\.pdf$", p.name).group(1)),
            )
            if page_paths:
                return len(page_paths), page_paths, None
    pages_dir = artifact_exam_pages_dir(artifact_dir) if artifact_dir is not None else None
    return split_pdf_into_pages(actual_exam_pdf, pages_dir)


def _rasterize_exam_pages(
    actual_exam_pdf: Path, n_pages: int,
) -> dict[int, bytes]:
    import fitz
    _dpi = int(os.environ.get("EXTRACT_EXAM_QUESTIONS_DPI", "300"))
    page_pngs: dict[int, bytes] = {}
    with fitz.open(str(actual_exam_pdf)) as _doc_r:
        for _i in range(n_pages):
            pix = _doc_r[_i].get_pixmap(dpi=_dpi)
            page_pngs[_i + 1] = pix.tobytes("png")
            pix = None
    return page_pngs


def _merge_fill_into_scaffold(
    scaffold_nodes: list[dict], by_number: dict[str, dict],
) -> None:
    """Walk *scaffold_nodes* in-place; for each node look up by ``number`` and
    copy ``text`` + ``answer_options`` from the matching fill entry."""
    for node in scaffold_nodes:
        num = str(node.get("number", "")).strip()
        entry = by_number.get(num)
        if entry is not None:
            node["text"] = entry.get("text", "")
            node["answer_options"] = entry.get("options") or []
        _merge_fill_into_scaffold(node.get("subquestions") or [], by_number)


def _run_truncation_check(scaffold_nodes: list[dict]) -> None:
    """Walk the scaffold and warn on truncation symptoms.

    Two reliable signals only:
    - leaf node with empty ``text``
    - ``multiple_choice`` node with no options
    """
    findings: list[str] = []

    def visit(node: dict) -> None:
        num = str(node.get("number", "") or "?")
        subs = node.get("subquestions") or []
        text_empty = not str(node.get("text") or "").strip()
        if not subs and text_empty:
            findings.append(f"q{num}: empty text on leaf")
        if (str(node.get("question_type", "")) == "multiple_choice"
                and not (node.get("answer_options") or [])):
            findings.append(f"q{num}: type=multiple_choice but no options")
        for s in subs:
            visit(s)

    for q in scaffold_nodes:
        visit(q)

    for f in findings:
        warn_line(f"Fill: {f}")


def extract_exam_questions(
    client,
    fill_model: str,
    fill_thinking: int | None,
    fill_max_tokens: int | None,
    *,
    actual_exam_pdf: Path,
    scaffold_nodes: list[dict],
    artifact_dir: "Path | None",
    fmt=None,
    is_cs: bool = False,
    should_cache: bool = False,
) -> list[dict]:
    """Per-page parallel extract of question text + options. Returns *scaffold_nodes* (mutated in place)."""
    if fmt is None:
        from xscore.scaffold.formats.base import ScaffoldFormat
        fmt = ScaffoldFormat()

    expected = _expected_qnums_by_page(scaffold_nodes)
    n_pages, page_paths, _tmp_dir = _ensure_exam_pages(actual_exam_pdf, artifact_dir)

    # OpenAI-compatible client path. Kimi extracts PDFs to text (no PNG needed);
    # qwen-doc-turbo / qwen-long take native PDFs via fileid://; everything
    # else (Grok, qwen3-vl-plus, …) needs rasterized PNGs per page.
    _oa_client = None
    _oa_provider = ""
    _oa_use_stream = False
    _oa_thinking_kw: dict = {}
    _use_qwen_pdf = False
    page_pngs: dict[int, bytes] = {}
    if not fill_model.startswith("gemini"):
        _oa_result = make_ai_client(
            model_env="EXTRACT_EXAM_QUESTIONS_MODEL", should_cache=should_cache,
        )
        if _oa_result is None:
            raise RuntimeError(
                f"No API key set for extract-questions model {fill_model!r}"
            )
        _oa_client, _, _oa_provider, _, _ = _oa_result
        _oa_use_stream, _oa_thinking_kw = build_completion_kwargs(
            _oa_provider, fill_thinking, fill_max_tokens,
        )
        _use_qwen_pdf = _oa_provider == "qwen" and model_supports_pdf_input(fill_model)
        if _oa_provider != "kimi" and not _use_qwen_pdf:
            page_pngs = _rasterize_exam_pages(actual_exam_pdf, n_pages)

    page_path_by_num: dict[int, Path] = {pn: p for pn, p in enumerate(page_paths, 1)}

    info_line(f"Filling exam text on {n_pages} page(s) ({fill_model}) …")

    def _call_page(page_num: int) -> dict[str, dict]:
        qnums = expected.get(page_num) or []
        if not qnums:
            ok_line(f"p{page_num}   skipped (no questions)")
            return {}

        filtered = _filter_questions_by_qnums(scaffold_nodes, set(qnums))
        stub = fmt.build_questions_stub(filtered)
        if _oa_client is None:
            _input_label = "PDF"
        elif _oa_provider == "kimi":
            _input_label = "PDF"
        elif _use_qwen_pdf:
            _input_label = "PDF"
        else:
            _input_label = "image"
        user_msg = fmt.build_questions_user_msg(
            stub, page_num, n_pages, qnums, input_label=_input_label,
        )

        # Hoist messages construction so it feeds both the API call and the
        # audit log. For the Gemini path build a parallel OpenAI-shape audit
        # list mirroring what the native Part-based call sends.
        from xscore.shared.prompt_logger import attachment_part
        _messages: list = []
        if _oa_client is None:
            _audit_messages: list = [
                {"role": "system", "content": fmt.system_questions_prompt(is_cs=is_cs)},
                {"role": "user", "content": [
                    attachment_part(
                        page_path_by_num[page_num].read_bytes(), "application/pdf"),
                    {"type": "text", "text": user_msg},
                ]},
            ]
        else:
            if _oa_provider == "kimi":
                _page_text = kimi_pdf_text(
                    _oa_client, page_path_by_num[page_num],
                    label=f"fill p{page_num}",
                )
                _messages = [
                    {"role": "system", "content": fmt.system_questions_prompt(is_cs=is_cs)},
                    {"role": "system", "content": _page_text},
                    {"role": "user", "content": user_msg},
                ]
            elif _use_qwen_pdf:
                file_id = upload_pdf_for_extract(
                    _oa_client, page_path_by_num[page_num],
                )
                _messages = [
                    {"role": "system", "content": fmt.system_questions_prompt(is_cs=is_cs)},
                    qwen_pdf_system_message(file_id),
                    {"role": "user", "content": user_msg},
                ]
            else:
                b64 = _base64.b64encode(page_pngs[page_num]).decode()
                _messages = [
                    {"role": "system", "content": fmt.system_questions_prompt(is_cs=is_cs)},
                    {"role": "user", "content": [
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        {"type": "text", "text": user_msg},
                    ]},
                ]
            _audit_messages = _messages

        def _do_call() -> tuple[str, str]:
            if _oa_client is not None:
                kwargs: dict = dict(model=fill_model, messages=_messages)
                kwargs.update(_oa_thinking_kw)
                if _oa_use_stream:
                    _th: list[str] = []
                    stream = _oa_client.chat.completions.create(**kwargs, stream=True)
                    _raw = collect_streamed_response(stream, thinking_out=_th)
                    return _raw, "".join(_th)
                _resp = _oa_client.chat.completions.create(**kwargs)
                return (
                    _resp.choices[0].message.content or "",
                    getattr(_resp.choices[0].message, "reasoning_content", "") or "",
                )
            _resp = client.models.generate_content(
                model=fill_model,
                contents=[
                    gemini_pdf_part(
                        client, page_path_by_num[page_num],
                        label=f"fill p{page_num}",
                    ),
                    gai_types.Part.from_text(text=user_msg),
                ],
                config=_make_gen_config(
                    fill_thinking, fmt.system_questions_prompt(is_cs=is_cs),
                    max_tokens=fill_max_tokens,
                ),
            )
            _raw, _th = split_gemini_response(_resp)
            return _raw, _th

        _t0 = time.perf_counter()
        try:
            raw, thinking_text = retry_api_call(
                _do_call, label=f"Fill p{page_num}",
            )
        except Exception as _exc:
            warn_line(
                f"p{page_num}   retries exhausted  ·  "
                f"{format_duration(time.perf_counter() - _t0)}  —  {_exc}"
            )
            return {}

        if artifact_dir is not None:
            _prompt_path = artifact_scaffold_prompt_path(
                artifact_dir, f"exam_questions_p{page_num}",
            )
            save_prompt(
                _prompt_path,
                model=fill_model, messages=_audit_messages,
            )
            save_input_data(_prompt_path, stub, ext="yaml")
            save_response(_prompt_path, raw or "", thinking=thinking_text)
            if raw:
                save_output_data(_prompt_path, raw, ext="yaml")

        _duration = format_duration(time.perf_counter() - _t0)
        try:
            entries = fmt.parse_questions_response(raw or "")
        except RuntimeError as _exc:
            warn_line(f"p{page_num}   parse error  ·  {_duration}  —  {_exc}")
            return {}

        by_number: dict[str, dict] = {}
        for e in entries:
            num = str(e.get("number", "")).strip()
            if num:
                by_number[num] = e

        filled = [q for q in qnums if q in by_number]
        missing = [q for q in qnums if q not in by_number]
        chars = sum(len(str(e.get("text", "") or "")) for e in by_number.values())
        chars_str = f"{chars / 1000:.1f}k chars" if chars >= 1000 else f"{chars} chars"

        if not missing:
            ok_line(
                f"p{page_num}   {_format_qnums_for_line(filled)}  "
                f"·  {_duration}  ·  {chars_str}"
            )
        else:
            warn_line(
                f"p{page_num}   {_format_qnums_for_line(filled) or '—'} filled  ·  "
                f"{_format_qnums_for_line(missing)} missing  ·  {_duration}"
            )
        return by_number

    workers = min(
        n_pages, int(os.environ.get("EXTRACT_EXAM_QUESTIONS_WORKERS", "500")),
    )
    with ThreadPoolExecutor(max_workers=workers) as pool:
        per_page = list(pool.map(_call_page, range(1, n_pages + 1)))

    merged: dict[str, dict] = {}
    for page_dict in per_page:
        merged.update(page_dict)

    _merge_fill_into_scaffold(scaffold_nodes, merged)
    _run_truncation_check(scaffold_nodes)

    if _tmp_dir is not None:
        import shutil
        shutil.rmtree(_tmp_dir, ignore_errors=True)

    return scaffold_nodes
