"""Parse the student roster from any supported file format via Gemini."""

from __future__ import annotations

import csv
import io
import json
import os
import time
from pathlib import Path

_PROMPT = (
    "Extract all student names from this data. "
    "Return a JSON array of name strings only — no numbers, headers, or extra text."
)


def _read_model_config() -> tuple[str, int | None, int | None]:
    from eXercise.ai_client import parse_model_spec
    raw = os.getenv("03_READ_STUDENT_LIST_MODEL", os.getenv("AI_DEFAULT_MODEL", "gemini-2.5-flash"))
    return parse_model_spec(raw)


_SHEET_KEYWORDS = ["student list", "student", "roster", "class list", "participants", "names"]


def _best_sheet(wb):
    """Return the worksheet whose name best matches a student-list keyword."""
    if len(wb.sheetnames) == 1:
        return wb.active
    for keyword in _SHEET_KEYWORDS:
        for name in wb.sheetnames:
            if keyword in name.lower():
                return wb[name]
    return wb.active or wb.worksheets[0]


def _spreadsheet_to_csv(path: Path) -> str:
    """Convert Excel or CSV to a plain CSV string."""
    ext = path.suffix.lower()
    if ext in (".xlsx", ".xls"):
        try:
            import openpyxl
        except ImportError:
            raise ImportError("openpyxl is required: pip install openpyxl>=3.1.0")
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = _best_sheet(wb)
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in ws.iter_rows(values_only=True):
            writer.writerow([str(v) if v is not None else "" for v in row])
        wb.close()
        return buf.getvalue()
    elif ext == ".csv":
        return path.read_text(errors="replace")
    raise ValueError(f"Unsupported spreadsheet format: {path.suffix}")


def _parse_name_list(raw: str) -> list[str]:
    """Parse a list of names from either a bare JSON array or ``{"names": [...]}``."""
    data = json.loads(raw)
    if isinstance(data, list):
        return [str(x) for x in data]
    if isinstance(data, dict):
        for k in ("names", "students", "student_names", "items"):
            if k in data and isinstance(data[k], list):
                return [str(x) for x in data[k]]
    raise ValueError(f"Could not extract a name list from response: {raw[:200]!r}")


def read_student_list(folder: Path, artifact_dir: Path | None = None) -> list[str]:
    """Return student names from any student list file in *folder*.

    Supports .xlsx, .xls, .csv (text) and .pdf (Gemini Files API; rasterized PNG
    pages on OpenAI-compat). Uses ``READ_STUDENT_LIST_MODEL`` (or
    ``AI_DEFAULT_MODEL``) to choose the provider.

    Raises FileNotFoundError if no student list file is found.
    """
    candidates = list(folder.glob("StudentList.*"))
    if not candidates:
        for pat in ("*[Ss]tudent*", "*[Rr]oster*"):
            candidates = list(folder.glob(pat))
            if candidates:
                break
    if not candidates:
        raise FileNotFoundError(f"No student list file found in {folder}")

    preferred = [f for f in candidates if "student" in f.name.lower()]
    target = preferred[0] if preferred else candidates[0]
    ext = target.suffix.lower()

    if ext not in (".xlsx", ".xls", ".csv", ".pdf"):
        raise ValueError(
            f"Unsupported student list format: {ext}. "
            "Supported: .xlsx, .xls, .csv, .pdf"
        )

    model_name, thinking_tokens, max_tokens = _read_model_config()

    from xscore.shared.terminal_ui import api_latency_line
    _save_prompt_path = None
    if artifact_dir is not None:
        from xscore.shared.exam_paths import artifact_student_list_prompt_path
        _save_prompt_path = artifact_student_list_prompt_path(artifact_dir)

    if model_name.startswith("gemini"):
        try:
            from google.genai import types as gai_types
        except ImportError:
            raise RuntimeError("google-genai not installed; run: pip install google-genai")
        from eXercise.ai_client import build_gemini_thinking_config, make_gemini_native_client
        client = make_gemini_native_client()
        if client is None:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")

        gen_config_kwargs: dict = {
            "max_output_tokens": max_tokens or 2048,
            "response_mime_type": "application/json",
            "response_schema": list[str],
        }
        if thinking_tokens is not None:
            gen_config_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
        gen_config = gai_types.GenerateContentConfig(**gen_config_kwargs)

        _prompt_user_text = ""
        if ext in (".xlsx", ".xls", ".csv"):
            csv_text = _spreadsheet_to_csv(target)
            _prompt_user_text = _PROMPT + "\n\n" + csv_text
            contents = [_prompt_user_text]
        else:  # .pdf
            uploaded = client.files.upload(file=target)
            for _ in range(180):  # up to 6 minutes at 2 s intervals
                if getattr(uploaded.state, "name", str(uploaded.state)) != "PROCESSING":
                    break
                time.sleep(2)
                uploaded = client.files.get(name=uploaded.name)
            else:
                raise TimeoutError(
                    f"Gemini file upload timed out after 6 min: {uploaded.name}"
                )
            if getattr(uploaded.state, "name", str(uploaded.state)) == "FAILED":
                raise RuntimeError(f"Gemini file processing failed: {uploaded.name}")
            _prompt_user_text = _PROMPT
            contents = [
                gai_types.Part.from_uri(file_uri=uploaded.uri, mime_type="application/pdf"),
                gai_types.Part.from_text(text=_PROMPT),
            ]

        if _save_prompt_path is not None:
            from xscore.shared.prompt_logger import save_prompt
            save_prompt(
                _save_prompt_path, model=model_name,
                messages=[{"role": "user", "content": _prompt_user_text}],
            )

        _t0 = time.perf_counter()
        response = client.models.generate_content(
            model=model_name,
            contents=contents,
            config=gen_config,
        )
        api_latency_line(time.perf_counter() - _t0, label="student list")
        raw = response.text or ""
    else:
        # OpenAI-compat path (Qwen, Grok, …): rasterize PDFs; send CSV as text.
        import base64 as _base64
        from eXercise.ai_client import (
            build_completion_kwargs,
            collect_streamed_response,
            make_ai_client,
            provider_for_model,
        )
        _result = make_ai_client(
            model_env="03_READ_STUDENT_LIST_MODEL",
            legacy_model_env="AI_DEFAULT_MODEL",
        )
        if _result is None:
            raise RuntimeError(
                f"03_READ_STUDENT_LIST_MODEL={model_name} requires the API key for "
                f"provider '{provider_for_model(model_name)}' in .env"
            )
        _oa_client, _, _provider, _, _ = _result
        _use_stream, _kw = build_completion_kwargs(
            _provider, thinking_tokens, max_tokens or 2048,
        )

        _oa_prompt = _PROMPT + (
            '\n\nReturn JSON only with this shape: {"names": [<str>, <str>, ...]}'
        )
        _prompt_user_text = ""
        if ext in (".xlsx", ".xls", ".csv"):
            csv_text = _spreadsheet_to_csv(target)
            _prompt_user_text = _oa_prompt + "\n\n" + csv_text
            user_content = _prompt_user_text
        else:  # .pdf — rasterize at 200 DPI to keep request size sane
            import fitz as _fitz
            _prompt_user_text = _oa_prompt + f"\n\n[PDF: {target.name}]"
            with _fitz.open(str(target)) as _doc:
                _pages_b64 = [
                    _base64.b64encode(_doc[i].get_pixmap(dpi=200).tobytes("png")).decode()
                    for i in range(_doc.page_count)
                ]
            user_content = [
                {"type": "text", "text": _oa_prompt},
            ] + [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
                for b64 in _pages_b64
            ]

        if _save_prompt_path is not None:
            from xscore.shared.prompt_logger import save_prompt
            save_prompt(
                _save_prompt_path, model=model_name,
                messages=[{"role": "user", "content": _prompt_user_text}],
            )

        _msgs = [{"role": "user", "content": user_content}]
        _t0 = time.perf_counter()
        if _use_stream:
            _stream = _oa_client.chat.completions.create(
                model=model_name, messages=_msgs, stream=True, **_kw,
            )
            raw = collect_streamed_response(_stream)
        else:
            try:
                _resp = _oa_client.chat.completions.create(
                    model=model_name, messages=_msgs,
                    response_format={"type": "json_object"}, **_kw,
                )
            except Exception:
                _resp = _oa_client.chat.completions.create(
                    model=model_name, messages=_msgs, **_kw,
                )
            raw = _resp.choices[0].message.content or ""
        api_latency_line(time.perf_counter() - _t0, label="student list")

    try:
        return _parse_name_list(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"Student-list API returned non-JSON (model={model_name}): {exc}\n"
            f"  Raw response: {raw!r:.200}"
        ) from exc
