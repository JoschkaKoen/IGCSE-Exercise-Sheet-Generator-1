"""Step 19 worker — extract_exam_question_numbers.

Single-call inference against the full split exam PDF. Returns the question
hierarchy with structural metadata only: number, type, page, subpage_row,
subpage_col, marks. **No text, no options.** Step 20 (extract_exam_questions)
populates those per-page in parallel.

Provider routing: Gemini inline PDF / Kimi PDF→text / Qwen ``fileid://``
(native PDF) / fallback PNG list.
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
from eXercise.api_retry import retry_api_call
from eXercise.qwen_input import (
    model_supports_pdf_input, qwen_pdf_system_message, upload_pdf_for_extract,
)
from xscore.scaffold.scaffold_api import _make_gen_config
from xscore.shared.exam_paths import (
    artifact_exam_scaffold_raw_path, artifact_scaffold_prompt_path,
)
from xscore.shared.prompt_logger import save_output_data, save_prompt, save_response
from xscore.shared.terminal_ui import api_latency_line, warn_line


def extract_exam_question_numbers(
    client,
    detect_model: str,
    detect_thinking: int | None,
    detect_max_tokens: int | None,
    *,
    actual_exam_pdf: Path,
    layout_result,
    split_pdf_path: "Path | None",
    n_split_pages: int,
    artifact_dir: "Path | None",
    fmt=None,
    is_cs: bool = False,
    should_cache: bool = False,
) -> tuple[list[dict], dict]:
    """Run the extract-question-numbers call. Returns ``(scaffold_nodes, layout_dict)``.

    Each node has number / type / page / subpage_row / subpage_col / marks /
    subquestions populated; ``text == ""`` and ``answer_options == []``.
    """
    if fmt is None:
        from xscore.scaffold.formats.base import ScaffoldFormat
        fmt = ScaffoldFormat()
    user_msg = fmt.build_question_numbers_user_msg(
        layout_result, split_pdf_path is not None, n_split_pages,
    )

    _oa_client = None
    _oa_provider = ""
    _oa_use_stream = False
    _oa_thinking_kw: dict = {}
    _oa_timeout_kw: dict = {}
    _use_qwen_pdf = False
    if not detect_model.startswith("gemini"):
        _oa_result = make_ai_client(
            model_env="EXTRACT_EXAM_QUESTION_NUMBERS_MODEL", should_cache=should_cache,
        )
        if _oa_result is None:
            raise RuntimeError(
                f"No API key set for extract-question-numbers model {detect_model!r}"
            )
        _oa_client, _, _oa_provider, _, _ = _oa_result
        _oa_use_stream, _oa_thinking_kw = build_completion_kwargs(
            _oa_provider, detect_thinking, detect_max_tokens,
        )
        from eXercise.ai_client import make_request_timeout  # noqa: PLC0415
        _oa_timeout = make_request_timeout("standard")
        _oa_timeout_kw = {"timeout": _oa_timeout} if _oa_timeout is not None else {}
        _use_qwen_pdf = (
            _oa_provider == "qwen" and model_supports_pdf_input(detect_model)
        )

    # Gemini   — inline PDF bytes via gemini_pdf_part.
    # Kimi     — server-extracted text via kimi_pdf_text (injected as system msg).
    # Qwen-PDF — upload PDF once, reference via fileid:// system message.
    # other OA — rasterize all pages to PNG (300 DPI by default).
    exam_pdf_part = None
    _exam_pdf_text = ""
    _exam_pdf_file_id = ""
    _exam_page_b64s: list[str] = []
    if _oa_client is None:
        exam_pdf_part = gemini_pdf_part(client, actual_exam_pdf, label="scaffold")
    elif _oa_provider == "kimi":
        _exam_pdf_text = kimi_pdf_text(_oa_client, actual_exam_pdf, label="scaffold")
    elif _use_qwen_pdf:
        _exam_pdf_file_id = upload_pdf_for_extract(_oa_client, actual_exam_pdf)
    else:
        import fitz as _fitz
        _dpi = int(os.environ.get("EXTRACT_EXAM_QUESTION_NUMBERS_DPI", "300"))
        with _fitz.open(str(actual_exam_pdf)) as _doc:
            for _i in range(_doc.page_count):
                pix = _doc[_i].get_pixmap(dpi=_dpi)
                _exam_page_b64s.append(_base64.b64encode(pix.tobytes("png")).decode())
                pix = None

    # Hoist the OpenAI-shape messages list so it feeds both the API call and
    # the audit log. For the Gemini path (no _oa_client), the API uses native
    # Part objects; build a parallel OpenAI-shape audit list mirroring it.
    from xscore.shared.prompt_logger import attachment_part
    _messages: list = []
    if _oa_client is None:
        _audit_messages: list = [
            {"role": "system", "content": fmt.system_question_numbers_prompt(is_cs=is_cs)},
            {"role": "user", "content": [
                attachment_part(actual_exam_pdf.read_bytes(), "application/pdf"),
                {"type": "text", "text": user_msg},
            ]},
        ]
    else:
        if _oa_provider == "kimi":
            _messages = [
                {"role": "system", "content": fmt.system_question_numbers_prompt(is_cs=is_cs)},
                {"role": "system", "content": _exam_pdf_text},
                {"role": "user", "content": user_msg},
            ]
        elif _use_qwen_pdf:
            _messages = [
                {"role": "system", "content": fmt.system_question_numbers_prompt(is_cs=is_cs)},
                qwen_pdf_system_message(_exam_pdf_file_id),
                {"role": "user", "content": user_msg},
            ]
        else:
            _content = [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                for b64 in _exam_page_b64s
            ]
            _content.append({"type": "text", "text": user_msg})
            _messages = [
                {"role": "system", "content": fmt.system_question_numbers_prompt(is_cs=is_cs)},
                {"role": "user", "content": _content},
            ]
        _audit_messages = _messages

    def _make_call(label: str | None) -> tuple[str, str]:
        def _do_call() -> tuple[str, str]:
            if _oa_client is not None:
                kwargs: dict = dict(model=detect_model, messages=_messages)
                kwargs.update(_oa_thinking_kw)
                kwargs.update(_oa_timeout_kw)
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
                model=detect_model,
                contents=[
                    exam_pdf_part,
                    gai_types.Part.from_text(text=user_msg),
                ],
                config=_make_gen_config(
                    detect_thinking, fmt.system_question_numbers_prompt(is_cs=is_cs),
                    max_tokens=detect_max_tokens,
                ),
            )
            _raw, _th = split_gemini_response(_resp)
            return _raw, _th

        _t0 = time.perf_counter()
        raw, thinking_text = retry_api_call(
            _do_call, label=f"Scaffold{f' ({label})' if label else ''}",
        )
        api_latency_line(time.perf_counter() - _t0, label=label)
        return raw, thinking_text

    raw, thinking_text = _make_call(None)
    if not raw:
        warn_line("Scaffold API: empty response — retrying once …")
        raw, thinking_text = _make_call("scaffold retry")
        if not raw:
            if artifact_dir is not None:
                try:
                    p = artifact_exam_scaffold_raw_path(artifact_dir, fmt=fmt.artifact_ext())
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text("# empty response after retry", encoding="utf-8")
                except OSError as e:
                    warn_line(f"Could not save empty-response stub: {e}")
            raise RuntimeError(f"Scaffold response empty after retry — {detect_model}")

    if artifact_dir is not None:
        _prompt_path = artifact_scaffold_prompt_path(artifact_dir, "exam_question_numbers")
        save_prompt(
            _prompt_path,
            model=detect_model, messages=_audit_messages,
        )
        save_response(_prompt_path, raw, thinking=thinking_text)
        save_output_data(_prompt_path, raw, ext="yaml")
        try:
            p = artifact_exam_scaffold_raw_path(artifact_dir, fmt=fmt.artifact_ext())
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(raw, encoding="utf-8")
        except OSError as e:
            warn_line(f"Could not save raw scaffold response: {e}")

    try:
        return fmt.parse_question_numbers_response(raw)
    except Exception as exc:
        raise RuntimeError(
            f"Scaffold response failed parsing: {exc}: {raw[:300]!r}"
        )
