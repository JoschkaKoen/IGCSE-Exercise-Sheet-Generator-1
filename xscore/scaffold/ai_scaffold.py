"""AI-based exam scaffold extraction via Gemini.

Replaces the PyMuPDF heuristic parsing with two Gemini API calls:
  Call 1 — exam PDF  → question hierarchy (text, marks, page, subquestions, MC options)
  Call 2 — mark-scheme PDF (optional) → flat list of correct_answer + marking_criteria

Returns list[Question] with spatial BBox zeroed (page coord preserved) so the
overlay PDF generator produces a clean copy of the exam PDF with no annotations.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from xscore.shared.exam_paths import (
    artifact_exam_questions_json_path,
    artifact_mark_scheme_json_path,
)
from xscore.shared.models import BBox, McAnswerOption, Question


# ---------------------------------------------------------------------------
# Model config — same pattern as load_student_list.py
# ---------------------------------------------------------------------------

def _parse_model(raw: str) -> tuple[str, str | None]:
    if "," in raw:
        model, effort = raw.split(",", 1)
        return model.strip(), effort.strip() or None
    return raw.strip(), None


def _exam_pdf_model_config() -> tuple[str, str | None]:
    return _parse_model(os.getenv("READ_EXAM_PDF_MODEL", os.getenv("AI_DEFAULT_MODEL", "gemini-2.5-flash")))


def _mark_scheme_model_config() -> tuple[str, str | None]:
    return _parse_model(os.getenv("READ_MARK_SCHEME_MODEL", os.getenv("AI_DEFAULT_MODEL", "gemini-2.5-flash")))


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_EXAM = (
    "You are an expert at reading Cambridge IGCSE exam papers. "
    "Extract every question and sub-question as structured JSON."
)

_USER_EXAM = """\
Extract the complete question hierarchy from this exam paper.

For EACH question and sub-question at EVERY nesting level return an object with:
- "number": the label as printed, in run-together form — top-level "9", then "9a", then "9ai", "9aii" (no parentheses, no spaces in the number)
- "question_type": one of "multiple_choice" | "short_answer" | "calculation" | "long_answer"
- "page": 1-based page number where this question first appears
- "marks": integer mark allocation from [N] brackets; 0 if not printed
- "text": complete question text in markdown; $...$ for inline math, $$...$$ for display math
- "answer_options": for multiple_choice only — [{"letter": "A", "text": "..."}, ...]; empty list otherwise
- "subquestions": list of child questions in the same format; empty list for leaf questions

Return ONLY valid JSON: {"questions": [...]}
"""

_SYSTEM_SCHEME = (
    "You are an expert at reading Cambridge IGCSE mark schemes. "
    "Extract marking criteria as structured JSON."
)

_USER_SCHEME = """\
Extract marking information for every question from this mark scheme.

Return a FLAT list — do not recreate nesting. For each entry:
- "number": question label in run-together form matching the exam paper (e.g. "9a", "9ai", "38")
- "correct_answer": model answer string; for multiple-choice just the letter ("A"/"B"/"C"/"D"); null if not applicable
- "mark_scheme": [{"mark": "B1/M1/A1/...", "criterion": "text"}, ...]

Return ONLY valid JSON: {"questions": [...]}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _norm(n: str) -> str:
    """Normalise a question number for matching: "(9)(a)(i)" → "9ai", "9A" → "9a"."""
    return re.sub(r"[()\s]", "", str(n)).lower()


def _merge_scheme(questions: list[dict], scheme_map: dict[str, dict]) -> None:
    """Recursively annotate *questions* in-place with correct_answer/marking_criteria."""
    for node in questions:
        key = _norm(node.get("number", ""))
        entry = scheme_map.get(key)
        if entry:
            node["correct_answer"] = entry.get("correct_answer")
            criteria_lines = [
                f"[{m.get('mark', '')}] {m.get('criterion', '')}"
                for m in (entry.get("mark_scheme") or [])
                if m.get("criterion")
            ]
            node["marking_criteria"] = "\n".join(criteria_lines) or None
        else:
            node.setdefault("correct_answer", None)
            node.setdefault("marking_criteria", None)
        _merge_scheme(node.get("subquestions") or [], scheme_map)


def _json_to_question(node: dict) -> Question:
    """Convert a raw JSON dict from Gemini into a Question dataclass."""
    page = max(1, int(node.get("page") or 1))
    return Question(
        number=str(node["number"]),
        question_type=node.get("question_type", "short_answer"),
        text=node.get("text", ""),
        marks=max(0, int(node.get("marks") or 0)),
        bbox=BBox(0.0, 0.0, 0.0, 0.0, page),   # page preserved; spatial coords zeroed
        answer_options=[
            McAnswerOption(letter=str(o["letter"]), text=str(o.get("text") or ""))
            for o in (node.get("answer_options") or [])
            if isinstance(o, dict) and o.get("letter")
        ],
        subquestions=[_json_to_question(s) for s in (node.get("subquestions") or [])],
        correct_answer=node.get("correct_answer"),
        marking_criteria=node.get("marking_criteria"),
    )


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _upload_and_poll(client, path: Path, label: str):
    """Upload *path* to the Gemini Files API, poll until ACTIVE, return the file object."""
    f = client.files.upload(file=path)
    while getattr(f.state, "name", str(f.state)) == "PROCESSING":
        time.sleep(3)
        f = client.files.get(name=f.name)
    state = getattr(f.state, "name", str(f.state))
    if state == "FAILED":
        raise RuntimeError(f"Gemini file processing failed ({label}): {f.name}")
    return f


# ---------------------------------------------------------------------------
# Artifact helpers
# ---------------------------------------------------------------------------

def _save_exam_questions(artifact_dir: Path, raw_questions: list[dict]) -> None:
    """Write step-4 artifacts: ``4_exam_questions.json`` + ``4_exam_questions.md``."""
    from xscore.scaffold.scaffold_markdown import write_raw_exam_markdown
    json_path = artifact_exam_questions_json_path(artifact_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(raw_questions, f, indent=2, ensure_ascii=False)
    write_raw_exam_markdown(artifact_dir, raw_questions)


def _save_mark_scheme(artifact_dir: Path, scheme_questions: list[dict]) -> None:
    """Write step-5 artifacts: ``5_mark_scheme.json`` + ``5_mark_scheme.md``."""
    from xscore.scaffold.scaffold_markdown import write_mark_scheme_markdown
    json_path = artifact_mark_scheme_json_path(artifact_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(scheme_questions, f, indent=2, ensure_ascii=False)
    write_mark_scheme_markdown(artifact_dir, scheme_questions)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_ai_scaffold(
    exam_pdf: Path,
    marking_scheme_pdf: Path | None,
    *,
    on_exam_complete: "Callable[[list[dict]], None] | None" = None,
    artifact_dir: Path | None = None,
) -> list[Question]:
    """Extract exam structure via Gemini and return a list[Question].

    Args:
        exam_pdf: Path to the exam question-paper PDF.
        marking_scheme_pdf: Optional mark-scheme PDF; skipped when None.
        on_exam_complete: Optional callback invoked with the raw question dicts
            after the first API call (exam extraction) completes successfully.
            Use this to advance the pipeline step counter between the two calls.
        artifact_dir: If set, write intermediate JSON + Markdown snapshots:
            ``4_exam_questions.*`` after call 1, ``5_mark_scheme.*`` after call 2.
            Saves are best-effort; OSError is silently ignored.

    Returns:
        list[Question] with spatial BBox coordinates zeroed (page number preserved).

    Raises:
        RuntimeError: GOOGLE_API_KEY unset, file upload failed, or Gemini returns non-JSON.
    """
    try:
        from google import genai as gai
        from google.genai import types as gai_types
    except ImportError:
        raise RuntimeError("google-genai not installed; run: pip install google-genai")

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    exam_model, exam_effort = _exam_pdf_model_config()
    scheme_model, scheme_effort = _mark_scheme_model_config()
    client = gai.Client(api_key=api_key)

    thinking_map = {"off": 0, "low": 1024, "high": 8192}

    def _make_gen_config(effort: str | None, system: str) -> "gai_types.GenerateContentConfig":
        cfg: dict = {"max_output_tokens": 16384, "response_mime_type": "application/json"}
        if effort in thinking_map:
            cfg["thinking_config"] = gai_types.ThinkingConfig(
                thinking_budget=thinking_map[effort],
                include_thoughts=False,
            )
        return gai_types.GenerateContentConfig(system_instruction=system, **cfg)

    # ---- Upload PDFs in parallel ----------------------------------------
    pdfs_to_upload: list[tuple[str, Path]] = [("exam", exam_pdf)]
    if marking_scheme_pdf is not None:
        pdfs_to_upload.append(("scheme", marking_scheme_pdf))

    def _upload(item: tuple[str, Path]):
        label, path = item
        return label, _upload_and_poll(client, path, label)

    uploaded_files: dict[str, object] = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for label, f in pool.map(_upload, pdfs_to_upload):
            uploaded_files[label] = f

    try:
        from xscore.shared.terminal_ui import api_latency_line

        # ---- Call 1: exam extraction ------------------------------------
        exam_file = uploaded_files["exam"]
        _t0 = time.perf_counter()
        exam_response = client.models.generate_content(
            model=exam_model,
            contents=[
                gai_types.Part.from_uri(
                    file_uri=exam_file.uri, mime_type="application/pdf"
                ),
                gai_types.Part.from_text(text=_USER_EXAM),
            ],
            config=_make_gen_config(exam_effort, _SYSTEM_EXAM),
        )
        api_latency_line(time.perf_counter() - _t0, label="exam")
        try:
            exam_data = json.loads(exam_response.text)
        except json.JSONDecodeError:
            raise RuntimeError(
                f"Gemini returned non-JSON for exam extraction: "
                f"{exam_response.text[:300]!r}"
            )
        if not isinstance(exam_data.get("questions"), list):
            raise RuntimeError(
                f"Gemini exam response missing 'questions' list: "
                f"{exam_response.text[:300]!r}"
            )
        raw_questions: list[dict] = exam_data["questions"]

        # Save step-4 artifacts BEFORE on_exam_complete — the callback may raise
        # SystemExit(0) when --through 4 is used, so anything after it won't run.
        if artifact_dir is not None:
            try:
                _save_exam_questions(artifact_dir, raw_questions)
            except OSError:
                pass

        if on_exam_complete is not None:
            on_exam_complete(raw_questions)

        # ---- Call 2: mark-scheme extraction (optional) ------------------
        if "scheme" in uploaded_files:
            scheme_file = uploaded_files["scheme"]
            _t0 = time.perf_counter()
            scheme_response = client.models.generate_content(
                model=scheme_model,
                contents=[
                    gai_types.Part.from_uri(
                        file_uri=scheme_file.uri, mime_type="application/pdf"
                    ),
                    gai_types.Part.from_text(text=_USER_SCHEME),
                ],
                config=_make_gen_config(scheme_effort, _SYSTEM_SCHEME),
            )
            api_latency_line(time.perf_counter() - _t0, label="mark scheme")
            try:
                scheme_data = json.loads(scheme_response.text)
            except json.JSONDecodeError:
                # Non-fatal: continue without mark-scheme annotations
                scheme_data = {"questions": []}

            if isinstance(scheme_data.get("questions"), list):
                # Save step-5 artifacts before merging — preserves the raw scheme output.
                if artifact_dir is not None:
                    try:
                        _save_mark_scheme(artifact_dir, scheme_data["questions"])
                    except OSError:
                        pass
                scheme_map = {
                    _norm(q.get("number", "")): q
                    for q in scheme_data["questions"]
                    if isinstance(q, dict) and q.get("number")
                }
                _merge_scheme(raw_questions, scheme_map)

    finally:
        # Delete uploaded files (auto-expire after 48 h anyway)
        for label, f in uploaded_files.items():
            try:
                client.files.delete(name=f.name)
            except Exception:
                pass

    return [_json_to_question(node) for node in raw_questions]
