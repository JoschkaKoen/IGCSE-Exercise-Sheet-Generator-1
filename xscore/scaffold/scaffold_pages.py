"""Step 23 — Assign questions to mark scheme pages (cheap per-page vision call).

Four-way provider routing: Gemini gets per-page PDFs inline; Kimi gets
server-extracted text via ``kimi_pdf_text`` injected as a system message;
qwen-doc-turbo / qwen-long get the per-page PDF via DashScope ``fileid://``
(native PDF); other OpenAI-compatible clients (Grok, ``qwen3-vl-plus``, …)
get rasterized PNGs. Skipped entirely when ``ASSIGN_SCHEME_QUESTIONS_MODEL``
is unset or step 19 produced no question numbers.
"""

from __future__ import annotations

import base64 as _base64
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from google.genai import types as gai_types

from eXercise.ai_client import (
    build_completion_kwargs, collect_streamed_response,
    gemini_pdf_part, kimi_pdf_text,
    make_ai_client, split_gemini_response,
)
from eXercise.qwen_input import (
    model_supports_pdf_input, qwen_pdf_system_message, upload_pdf_for_extract,
)
from eXercise.api_retry import retry_api_call
from xscore.prompts.loader import load_prompt
from xscore.scaffold.scaffold_api import _make_gen_config
from xscore.scaffold.scaffold_qtree import _collect_qnums, _leaf_qnums
from xscore.scaffold.scaffold_scheme_pdf import (
    _ensure_scheme_pages, _rasterize_scheme_pages,
)
from xscore.shared.exam_paths import (
    artifact_questions_per_page_path, artifact_scaffold_prompt_path,
)
from xscore.shared.prompt_logger import (
    save_output_data, save_prompt, save_response,
)
from xscore.shared.terminal_ui import (
    format_duration, info_line, ok_line, warn_line,
)


def assign_questions_to_pages(
    client,
    marking_scheme_pdf: Path,
    raw_questions: list[dict],
    artifact_dir: "Path | None",
) -> dict[int, list[str]]:
    """For each mark scheme page, ask a cheap vision model which question
    numbers' marking criteria appear on it.

    Returns ``{page_num: [qnum, ...]}``. Empty dict when the model env var is
    unset, no question numbers are available, or the call fails — the caller
    (step 24) then falls back to its full-scaffold behavior.

    Provider routing (auto-detected from ``ASSIGN_SCHEME_QUESTIONS_MODEL``):
      * ``gemini*``         → inline per-page PDFs via ``gemini_pdf_part``,
        ``client.models.generate_content`` with ``response_mime_type='application/json'``.
      * ``kimi*``/``moonshot*`` → per-page PDF extracted to text via
        ``kimi_pdf_text`` and injected as a system message; ``chat.completions.create``
        with ``response_format={"type": "json_object"}``.
      * everything else → rasterize PNGs, ``chat.completions.create`` on
        an OpenAI-compatible client with ``response_format={"type": "json_object"}``.
    """
    _result = make_ai_client(model_env="ASSIGN_SCHEME_QUESTIONS_MODEL")
    if _result is None:
        info_line("Skipped (ASSIGN_SCHEME_QUESTIONS_MODEL not set)")
        return {}
    _oa_client_aux, model, provider, thinking, max_tokens = _result

    qnums = _collect_qnums(raw_questions)
    if not qnums:
        info_line("Skipped (no question numbers from step 19)")
        return {}
    allowed = set(qnums)

    n_pages, page_paths, _tmp_dir = _ensure_scheme_pages(marking_scheme_pdf, artifact_dir)

    _, system_msg = load_prompt("assign_scheme_questions", section="system")
    _, user_msg = load_prompt(
        "assign_scheme_questions", section="user",
        question_numbers=", ".join(f'"{q}"' for q in qnums),
    )

    use_gemini = provider == "gemini"
    use_kimi = provider == "kimi"
    use_qwen_pdf = provider == "qwen" and model_supports_pdf_input(model)
    page_pngs: dict[int, bytes] = {}
    _oa_use_stream = False
    _oa_thinking_kw: dict = {}

    info_line(f"Assigning questions to {n_pages} page(s) ({model}) …")

    if not use_gemini:
        _oa_use_stream, _oa_thinking_kw = build_completion_kwargs(provider, thinking, max_tokens)
        if not use_kimi and not use_qwen_pdf:
            page_pngs = _rasterize_scheme_pages(marking_scheme_pdf, n_pages)

    page_path_by_num: dict[int, Path] = {pn: p for pn, p in enumerate(page_paths, 1)}

    def _assign_page(page_num: int) -> tuple[int, list[str]]:
        # Hoist messages construction so it feeds both the API call and the
        # audit log. For the Gemini path build a parallel OpenAI-shape audit
        # list mirroring what the native Part-based call sends.
        from xscore.shared.prompt_logger import attachment_part
        _messages: list = []
        if use_gemini:
            _audit_messages: list = [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": [
                    attachment_part(
                        page_path_by_num[page_num].read_bytes(), "application/pdf"),
                    {"type": "text", "text": user_msg},
                ]},
            ]
        elif use_kimi:
            _page_text = kimi_pdf_text(
                _oa_client_aux, page_path_by_num[page_num], label=f"assign p{page_num}",
            )
            _messages = [
                {"role": "system", "content": system_msg},
                {"role": "system", "content": _page_text},
                {"role": "user", "content": user_msg},
            ]
            _audit_messages = _messages
        elif use_qwen_pdf:
            file_id = upload_pdf_for_extract(
                _oa_client_aux, page_path_by_num[page_num],
            )
            _messages = [
                {"role": "system", "content": system_msg},
                qwen_pdf_system_message(file_id),
                {"role": "user", "content": user_msg},
            ]
            _audit_messages = _messages
        else:
            b64 = _base64.b64encode(page_pngs[page_num]).decode()
            _messages = [
                {"role": "system", "content": system_msg},
                {"role": "user", "content": [
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    {"type": "text", "text": user_msg},
                ]},
            ]
            _audit_messages = _messages

        def _do_call() -> tuple[str, str]:
            if use_gemini:
                _resp = client.models.generate_content(
                    model=model,
                    contents=[
                        gemini_pdf_part(client, page_path_by_num[page_num], label=f"assign p{page_num}"),
                        gai_types.Part.from_text(text=user_msg),
                    ],
                    config=_make_gen_config(
                        thinking, system_msg,
                        schema={
                            "type": "object",
                            "properties": {
                                "questions": {"type": "array", "items": {"type": "string"}}
                            },
                            "required": ["questions"],
                        },
                        max_tokens=max_tokens,
                    ),
                )
                _raw, _th = split_gemini_response(_resp)
                return _raw, _th
            # OpenAI-compat path. Dispatch streaming vs non-streaming uniformly
            # so K2 thinking-on (which forces streaming) is honoured here too —
            # non-streaming K2 thinking blocks the whole reply and looks like a
            # hang.
            kwargs: dict = dict(
                model=model,
                messages=_messages,
                response_format={"type": "json_object"},
            )
            kwargs.update(_oa_thinking_kw)
            if _oa_use_stream:
                _th: list[str] = []
                # Stream consumed inside the closure so a mid-stream SSL EOF
                # triggers a retry rather than returning a partial response.
                # Thinking lands in `_th` (saved to file via save_response
                # below); content streams silently to keep terminal clean.
                stream = _oa_client_aux.chat.completions.create(**kwargs, stream=True)
                _raw = collect_streamed_response(stream, thinking_out=_th)
                return _raw or '{"questions":[]}', "".join(_th)
            _resp = _oa_client_aux.chat.completions.create(**kwargs)
            return (
                _resp.choices[0].message.content or '{"questions":[]}',
                getattr(_resp.choices[0].message, "reasoning_content", "") or "",
            )

        _t0 = time.perf_counter()
        try:
            raw, thinking_text = retry_api_call(
                _do_call, label=f"Assign questions p{page_num}",
            )
        except Exception as _exc:
            warn_line(
                f"Assign questions p{page_num}: giving up after retries  ·  "
                f"{format_duration(time.perf_counter() - _t0)}  —  {_exc}"
            )
            return page_num, []

        if artifact_dir is not None:
            _prompt_path = artifact_scaffold_prompt_path(
                artifact_dir, f"assign_scheme_questions_p{page_num}"
            )
            save_prompt(_prompt_path, model=model, messages=_audit_messages)
            save_response(_prompt_path, raw or "", thinking=thinking_text)
            if raw:
                save_output_data(_prompt_path, raw, ext="json")

        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            ok_line(f"p{page_num}  ·  parse error  ·  {format_duration(time.perf_counter() - _t0)}")
            return page_num, []
        result = [str(q) for q in (data.get("questions") or []) if str(q) in allowed]
        _qs_str = (", ".join(f"q{q}" for q in result)) if result else "—"
        ok_line(f"p{page_num}  ·  {_qs_str}  ·  {format_duration(time.perf_counter() - _t0)}")
        return page_num, result

    with ThreadPoolExecutor(max_workers=min(n_pages, int(os.environ.get("ASSIGN_SCHEME_QUESTIONS_WORKERS", "500")))) as pool:
        pairs = list(pool.map(_assign_page, range(1, n_pages + 1)))

    mapping: dict[int, list[str]] = {pn: qs for pn, qs in pairs}

    # Warn about leaf questions the AI never assigned to any page — step 24
    # will skip these, so the final mark scheme will have no criteria for them.
    _assigned: set[str] = set()
    for _qs in mapping.values():
        _assigned.update(_qs)
    _missing = [q for q in _leaf_qnums(raw_questions) if q not in _assigned]
    if _missing:
        warn_line(
            f"Mark scheme: {len(_missing)} question(s) not assigned to any page "
            f"(step 24 will skip them): "
            + ", ".join(f"q{q}" for q in _missing)
        )

    if artifact_dir is not None:
        try:
            p = artifact_questions_per_page_path(artifact_dir)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps(
                    {str(k): v for k, v in sorted(mapping.items())},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as e:
            warn_line(f"Could not save questions_per_page.json: {e}")

    if _tmp_dir is not None:
        import shutil
        shutil.rmtree(_tmp_dir, ignore_errors=True)

    return mapping
