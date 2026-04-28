"""Step 18 — Parse the exam PDF into raw question entries.

Single-call inference (no per-page parallelism). Four-way provider routing:
Gemini gets the inline PDF via ``gemini_pdf_part``; Kimi gets the
server-extracted text via ``kimi_pdf_text`` injected as a system message;
qwen-doc-turbo / qwen-long get the exam PDF via DashScope ``fileid://``
(native PDF); other OpenAI-compatible clients (Grok, ``qwen3-vl-plus``, …)
get rasterized PNGs base64-encoded.
"""

from __future__ import annotations

import base64 as _base64
import os
import time
from pathlib import Path

from google.genai import types as gai_types

from eXercise.ai_client import (
    build_completion_kwargs, collect_streamed_response,
    gemini_pdf_part, kimi_pdf_text, make_ai_client, split_gemini_response,
)
from eXercise.qwen_input import (
    model_supports_pdf_input, qwen_pdf_system_message, upload_pdf_for_extract,
)
from eXercise.api_retry import retry_api_call
from xscore.scaffold.scaffold_api import _make_gen_config
from xscore.shared.exam_paths import (
    artifact_exam_questions_raw_path, artifact_scaffold_prompt_path,
)
from xscore.shared.prompt_logger import save_prompt, save_response
from xscore.shared.terminal_ui import api_latency_line, warn_line


def _do_exam_call(
    client,
    exam_model: str,
    exam_thinking: int | None,
    exam_max_tokens: int | None,
    *,
    actual_exam_pdf: Path,
    layout_result,
    split_pdf_path: "Path | None",
    n_split_pages: int,
    artifact_dir: "Path | None",
    fmt=None,
) -> tuple[list[dict], dict]:
    if fmt is None:
        from xscore.scaffold.formats.xml_format import XmlScaffoldFormat
        fmt = XmlScaffoldFormat()
    user_exam = fmt.build_exam_prompt(layout_result, split_pdf_path is not None, n_split_pages)

    # Non-Gemini path: OpenAI-compatible client (Kimi / Qwen / Grok / …)
    _oa_client = None
    _oa_provider = ""
    _oa_use_stream = False
    _oa_thinking_kw: dict = {}
    _use_qwen_pdf = False
    if not exam_model.startswith("gemini"):
        _oa_result = make_ai_client(model_env="READ_EXAM_PDF_MODEL")
        if _oa_result is None:
            raise RuntimeError(f"No API key set for exam model {exam_model!r}")
        _oa_client, _, _oa_provider, _, _ = _oa_result
        _oa_use_stream, _oa_thinking_kw = build_completion_kwargs(
            _oa_provider, exam_thinking, exam_max_tokens
        )
        _use_qwen_pdf = _oa_provider == "qwen" and model_supports_pdf_input(exam_model)

    # Gemini:   inline PDF bytes via gemini_pdf_part.
    # Kimi:     server-extracted text via kimi_pdf_text (injected as system msg).
    # Qwen-PDF: upload PDF once, reference via fileid:// system message.
    # other OA: rasterize all pages to PNG (300 DPI by default).
    exam_pdf_part = None
    _exam_pdf_text = ""
    _exam_pdf_file_id = ""
    _exam_page_b64s: list[str] = []
    if _oa_client is None:
        exam_pdf_part = gemini_pdf_part(client, actual_exam_pdf, label="exam")
    elif _oa_provider == "kimi":
        _exam_pdf_text = kimi_pdf_text(_oa_client, actual_exam_pdf, label="exam")
    elif _use_qwen_pdf:
        _exam_pdf_file_id = upload_pdf_for_extract(_oa_client, actual_exam_pdf)
    else:
        import fitz as _fitz
        _dpi = int(os.environ.get("READ_EXAM_PDF_DPI", "300"))
        with _fitz.open(str(actual_exam_pdf)) as _doc:
            for _i in range(_doc.page_count):
                pix = _doc[_i].get_pixmap(dpi=_dpi)
                _exam_page_b64s.append(_base64.b64encode(pix.tobytes("png")).decode())
                pix = None  # release pixmap memory

    def _make_exam_call(label: str | None) -> tuple[str, str]:
        def _do_call() -> tuple[str, str]:
            if _oa_client is not None:
                if _oa_provider == "kimi":
                    _messages = [
                        {"role": "system", "content": fmt.system_exam_prompt()},
                        {"role": "system", "content": _exam_pdf_text},
                        {"role": "user", "content": user_exam},
                    ]
                elif _use_qwen_pdf:
                    _messages = [
                        {"role": "system", "content": fmt.system_exam_prompt()},
                        qwen_pdf_system_message(_exam_pdf_file_id),
                        {"role": "user", "content": user_exam},
                    ]
                else:
                    _content = [
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                        for b64 in _exam_page_b64s
                    ]
                    _content.append({"type": "text", "text": user_exam})
                    _messages = [
                        {"role": "system", "content": fmt.system_exam_prompt()},
                        {"role": "user", "content": _content},
                    ]
                kwargs: dict = dict(
                    model=exam_model,
                    messages=_messages,
                )
                kwargs.update(_oa_thinking_kw)
                if _oa_use_stream:
                    _th: list[str] = []
                    # Stream consumed *inside* the closure so a mid-stream SSL EOF
                    # triggers a retry rather than returning a partial response.
                    stream = _oa_client.chat.completions.create(**kwargs, stream=True)
                    _raw = collect_streamed_response(stream, thinking_out=_th)
                    return _raw, "".join(_th)
                _resp = _oa_client.chat.completions.create(**kwargs)
                return (
                    _resp.choices[0].message.content or "",
                    getattr(_resp.choices[0].message, "reasoning_content", "") or "",
                )
            _resp = client.models.generate_content(
                model=exam_model,
                contents=[
                    exam_pdf_part,
                    gai_types.Part.from_text(text=user_exam),
                ],
                config=_make_gen_config(
                    exam_thinking, fmt.system_exam_prompt(),
                    pydantic_schema=fmt.pydantic_schema_exam(),
                    max_tokens=exam_max_tokens,
                ),
            )
            _raw, _th = split_gemini_response(_resp)
            return _raw, _th

        _t0 = time.perf_counter()
        raw, thinking_text = retry_api_call(
            _do_call, label=f"Exam{f' ({label})' if label else ''}",
        )
        api_latency_line(time.perf_counter() - _t0, label=label)
        return raw, thinking_text

    try:
        raw_exam, exam_thinking_text = _make_exam_call(None)
        if not raw_exam:
            warn_line("Exam API: empty response — retrying once …")
            raw_exam, exam_thinking_text = _make_exam_call("exam retry")
            if not raw_exam:
                if artifact_dir is not None:
                    try:
                        p = artifact_exam_questions_raw_path(artifact_dir, fmt=fmt.artifact_ext())
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text("# empty response after retry", encoding="utf-8")
                    except OSError as e:
                        warn_line(f"Could not save empty-response stub: {e}")
                raise RuntimeError(f"Exam response empty after retry — {exam_model}")
        if artifact_dir is not None:
            if _oa_client is None:
                _src_kind = "PDF (gemini)"
            elif _oa_provider == "kimi":
                _src_kind = "PDF→text (kimi)"
            elif _use_qwen_pdf:
                _src_kind = "PDF (qwen fileid)"
            else:
                _src_kind = "PNG list"
            _exam_prompt_path = artifact_scaffold_prompt_path(artifact_dir, "exam_questions")
            save_prompt(
                _exam_prompt_path,
                model=exam_model, system=fmt.system_exam_prompt(),
                messages=[{
                    "role": "user",
                    "content": f"[{_src_kind}: {actual_exam_pdf.name}]\n\n{user_exam}",
                }],
            )
            save_response(_exam_prompt_path, raw_exam, thinking=exam_thinking_text)
            try:
                p = artifact_exam_questions_raw_path(artifact_dir, fmt=fmt.artifact_ext())
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(raw_exam, encoding="utf-8")
            except OSError as e:
                warn_line(f"Could not save raw exam response: {e}")
        try:
            return fmt.parse_exam_response(raw_exam)
        except Exception as exc:
            raise RuntimeError(
                f"Exam response failed parsing: {exc}: {raw_exam[:300]!r}"
            )
    finally:
        # Inline PDF path: nothing to clean up. Files-API fallback (>18 MB)
        # auto-expires server-side after 48 h.
        pass
