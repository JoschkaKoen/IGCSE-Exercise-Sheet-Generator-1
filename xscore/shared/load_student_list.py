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


def _read_model_config() -> tuple[str, str | None]:
    raw = os.getenv("READ_STUDENT_LIST_MODEL", os.getenv("AI_DEFAULT_MODEL", "gemini-2.5-flash"))
    if "," in raw:
        model, effort = raw.split(",", 1)
        return model.strip(), effort.strip() or None
    return raw.strip(), None


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


def read_student_list(folder: Path, artifact_dir: Path | None = None) -> list[str]:
    """Return student names from any student list file in *folder*.

    Supports .xlsx, .xls, .csv (converted to CSV text) and .pdf (File API).
    Uses STUDENT_LIST_MODEL (or AI_DEFAULT_MODEL) to extract names via Gemini.
    JSON mode enforces structured output.

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

    model_name, effort = _read_model_config()

    try:
        from google.genai import types as gai_types
    except ImportError:
        raise RuntimeError("google-genai not installed; run: pip install google-genai")

    from eXercise.ai_client import make_gemini_native_client
    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")

    thinking_map = {"off": 0, "low": 1024, "high": 8192}
    thinking_cfg = None
    if effort in thinking_map:
        thinking_cfg = gai_types.ThinkingConfig(
            thinking_budget=thinking_map[effort],
            include_thoughts=False,
        )

    gen_config_kwargs: dict = {
        "max_output_tokens": 2048,
        "response_mime_type": "application/json",
        "response_schema": list[str],
    }
    if thinking_cfg:
        gen_config_kwargs["thinking_config"] = thinking_cfg
    gen_config = gai_types.GenerateContentConfig(**gen_config_kwargs)

    _prompt_user_text = ""
    if ext in (".xlsx", ".xls", ".csv"):
        csv_text = _spreadsheet_to_csv(target)
        _prompt_user_text = _PROMPT + "\n\n" + csv_text
        contents = [_prompt_user_text]
    elif ext == ".pdf":
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
    else:
        raise ValueError(
            f"Unsupported student list format: {ext}. "
            "Supported: .xlsx, .xls, .csv, .pdf"
        )

    from xscore.shared.terminal_ui import api_latency_line
    if artifact_dir is not None:
        from xscore.shared.exam_paths import artifact_prompt_path
        from xscore.shared.prompt_logger import save_prompt
        save_prompt(
            artifact_prompt_path(artifact_dir, "3_student_list"),
            model=model_name,
            messages=[{"role": "user", "content": _prompt_user_text}],
        )
    _t0 = time.perf_counter()
    response = client.models.generate_content(
        model=model_name,
        contents=contents,
        config=gen_config,
    )
    api_latency_line(time.perf_counter() - _t0, label="student list")
    try:
        return json.loads(response.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Student-list API returned non-JSON (model={model_name}): {exc}\n"
            f"  Raw response: {response.text!r:.200}"
        ) from exc
