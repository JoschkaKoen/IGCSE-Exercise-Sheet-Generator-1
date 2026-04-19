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
    artifact_prompt_path,
)
from xscore.shared.models import BBox, ExamLayout, McAnswerOption, Question
from xscore.shared.prompt_logger import save_prompt


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
First identify the page layout — how many sub-pages (quadrants) are arranged on \
each physical PDF page. Return a top-level "layout" object:
  {"rows": 1 or 2, "cols": 1 or 2}
A standard single-page exam has rows=1, cols=1. A 4-up exam (2×2 grid printed on one sheet) has rows=2, cols=2.

Then extract the complete question hierarchy from this exam paper.

For EACH question and sub-question at EVERY nesting level return an object with:
- "number": the label as printed, in run-together form — top-level "9", then "9a", then "9ai", "9aii" (no parentheses, no spaces in the number)
- "question_type": one of "multiple_choice" | "short_answer" | "calculation" | "long_answer"
- "page": 1-based page number where this question first appears
- "subpage_row": 1-based row of the sub-page quadrant this question is in \
(always 1 for a 1×1 layout; 1=top row, 2=bottom row for a 2×2 layout)
- "subpage_col": 1-based column of the sub-page quadrant this question is in \
(always 1 for a 1×1 layout; 1=left col, 2=right col for a 2×2 layout)
- "marks": integer mark allocation from [N] brackets; 0 if not printed
- "text": complete question text in markdown; $...$ for inline math, $$...$$ for display math
- "answer_options": for multiple_choice only — [{"letter": "A", "text": "..."}, ...]; empty list otherwise
- "subquestions": list of child questions in the same format; empty list for leaf questions

Return ONLY valid JSON: {"layout": {"rows": N, "cols": M}, "questions": [...]}
Use proper JSON escape sequences in all strings (\\n for newlines, \\t for tabs) — never embed literal control characters.
"""

_SYSTEM_SCHEME = (
    "You are an expert at reading Cambridge IGCSE mark schemes. "
    "Extract marking criteria as structured JSON."
)

_USER_SCHEME = """\
Extract marking information for every question from this mark scheme.

Return a FLAT list — do not recreate nesting. For each entry:
- "number": question label in run-together form matching the exam paper (e.g. "9a", "9ai", "38")
- "correct_answer": model answer in LaTeX-safe text — use $...$ for inline math (e.g. "$1.5 \\times 10^{11}$ m", "$v_0$"), \\% for percent signs, \\& for ampersands; for multiple-choice just the letter; null if not applicable
- "mark_scheme": [{"mark": "B1/M1/A1/etc. for Cambridge-typed marks, or \\\"\\\" (empty string) when the criterion has no specific mark type — never use JSON null for this field", "criterion": "text"}, ...]

Return ONLY valid JSON: {"questions": [...]}
Use proper JSON escape sequences (\\n for newlines) — strip all leading whitespace and tab characters from every criterion string; never start a criterion with \\t or any indentation.
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
            criteria_lines = []
            for m in (entry.get("mark_scheme") or []):
                criterion = m.get("criterion", "").lstrip("\t ")
                if not criterion:
                    continue
                mark_label = m.get("mark") or ""
                prefix = f"[{mark_label}] " if mark_label else ""
                criteria_lines.append(f"{prefix}{criterion}")
            node["marking_criteria"] = "\n".join(criteria_lines) or None
        else:
            node.setdefault("correct_answer", None)
            node.setdefault("marking_criteria", None)
        _merge_scheme(node.get("subquestions") or [], scheme_map)


def _json_to_question(node: dict, layout: ExamLayout) -> Question:
    """Convert a raw JSON dict from Gemini into a Question dataclass."""
    page = max(1, int(node.get("page") or 1))
    subpage_row = min(max(1, int(node.get("subpage_row") or 1)), layout.rows)
    subpage_col = min(max(1, int(node.get("subpage_col") or 1)), layout.cols)
    return Question(
        number=str(node["number"]),
        question_type=node.get("question_type", "short_answer"),
        text=node.get("text", ""),
        marks=max(0, int(node.get("marks") or 0)),
        bbox=BBox(0.0, 0.0, 0.0, 0.0, page),   # page preserved; spatial coords zeroed
        subpage_row=subpage_row,
        subpage_col=subpage_col,
        answer_options=[
            McAnswerOption(letter=str(o["letter"]), text=str(o.get("text") or ""))
            for o in (node.get("answer_options") or [])
            if isinstance(o, dict) and o.get("letter")
        ],
        subquestions=[_json_to_question(s, layout) for s in (node.get("subquestions") or [])],
        correct_answer=node.get("correct_answer"),
        marking_criteria=node.get("marking_criteria"),
    )


# ---------------------------------------------------------------------------
# Upload helpers
# ---------------------------------------------------------------------------

def _upload_and_poll(client, path: Path, label: str):
    """Upload *path* to the Gemini Files API, poll until ACTIVE, return the file object."""
    f = client.files.upload(file=path)
    for _ in range(120):  # up to 6 minutes at 3 s intervals
        if getattr(f.state, "name", str(f.state)) != "PROCESSING":
            break
        time.sleep(3)
        f = client.files.get(name=f.name)
    else:
        raise TimeoutError(f"Gemini file upload timed out after 6 min ({label}): {f.name}")
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
    on_scheme_complete: "Callable[[list[dict]], None] | None" = None,
    artifact_dir: Path | None = None,
) -> tuple[list[Question], ExamLayout]:
    """Extract exam structure via Gemini and return a list[Question].

    Args:
        exam_pdf: Path to the exam question-paper PDF.
        marking_scheme_pdf: Optional mark-scheme PDF; skipped when None.
        on_exam_complete: Optional callback invoked with the raw question dicts
            after the first API call (exam extraction) completes successfully.
            Use this to advance the pipeline step counter between the two calls.
        on_scheme_complete: Optional callback invoked with the raw scheme question
            dicts after the second API call completes, but *before* the scheme is
            merged into the question tree.  Use this to advance the step counter
            to the merge step.  May raise SystemExit(0) to stop before merging.
        artifact_dir: If set, write intermediate JSON + Markdown snapshots:
            ``4_exam_questions.*`` after call 1, ``5_mark_scheme.*`` after call 2.
            Saves are best-effort; OSError is silently ignored.

    Returns:
        Tuple of (list[Question], ExamLayout). Questions have spatial BBox coordinates
        zeroed (page and subpage numbers preserved).

    Raises:
        RuntimeError: GOOGLE_API_KEY unset, file upload failed, or Gemini returns non-JSON.
    """
    try:
        from google import genai as gai
        from google.genai import types as gai_types
    except ImportError:
        raise RuntimeError("google-genai not installed; run: pip install google-genai")

    api_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")

    exam_model, exam_effort = _exam_pdf_model_config()
    scheme_model, scheme_effort = _mark_scheme_model_config()
    client = gai.Client(api_key=api_key)

    thinking_map = {"off": 0, "low": 1024, "high": 8192}

    def _make_gen_config(effort: str | None, system: str) -> "gai_types.GenerateContentConfig":
        cfg: dict = {"max_output_tokens": 65536, "response_mime_type": "application/json"}
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

        # ---- Inference closures (called from threads or inline) -----------

        def _do_exam_call() -> tuple[list[dict], dict]:
            if artifact_dir is not None:
                save_prompt(
                    artifact_prompt_path(artifact_dir, "4_exam_questions"),
                    model=exam_model, system=_SYSTEM_EXAM,
                    messages=[{"role": "user", "content": _USER_EXAM}],
                )
            _t0 = time.perf_counter()
            resp = client.models.generate_content(
                model=exam_model,
                contents=[
                    gai_types.Part.from_uri(
                        file_uri=uploaded_files["exam"].uri, mime_type="application/pdf"
                    ),
                    gai_types.Part.from_text(text=_USER_EXAM),
                ],
                config=_make_gen_config(exam_effort, _SYSTEM_EXAM),
            )
            api_latency_line(time.perf_counter() - _t0, label="exam")
            try:
                data = json.loads(resp.text)
            except json.JSONDecodeError:
                raise RuntimeError(
                    f"Gemini returned non-JSON for exam extraction: {resp.text[:300]!r}"
                )
            if not isinstance(data.get("questions"), list):
                raise RuntimeError(
                    f"Gemini exam response missing 'questions' list: {resp.text[:300]!r}"
                )
            return data["questions"], data.get("layout") or {}

        def _do_scheme_call() -> dict:
            if artifact_dir is not None:
                save_prompt(
                    artifact_prompt_path(artifact_dir, "5_mark_scheme"),
                    model=scheme_model, system=_SYSTEM_SCHEME,
                    messages=[{"role": "user", "content": _USER_SCHEME}],
                )
            _t0 = time.perf_counter()
            resp = client.models.generate_content(
                model=scheme_model,
                contents=[
                    gai_types.Part.from_uri(
                        file_uri=uploaded_files["scheme"].uri, mime_type="application/pdf"
                    ),
                    gai_types.Part.from_text(text=_USER_SCHEME),
                ],
                config=_make_gen_config(scheme_effort, _SYSTEM_SCHEME),
            )
            api_latency_line(time.perf_counter() - _t0, label="mark scheme")
            try:
                return json.loads(resp.text)
            except json.JSONDecodeError:
                return {"questions": []}   # non-fatal

        # ---- Dispatch: parallel when scheme is present -------------------
        # Both PDFs are already uploaded; running the two Gemini inference
        # calls concurrently saves ~10–20 s (the duration of the shorter call).
        # rich.Console.print() holds an internal lock so api_latency_line is
        # safe to call from worker threads.
        raw_layout: dict = {}
        if "scheme" in uploaded_files:
            with ThreadPoolExecutor(max_workers=2) as _ex:
                _exam_fut = _ex.submit(_do_exam_call)
                _scheme_fut = _ex.submit(_do_scheme_call)
            raw_questions, raw_layout = _exam_fut.result()   # propagates RuntimeError on failure
            try:
                scheme_data: dict = _scheme_fut.result()
            except Exception:
                scheme_data = {"questions": []}   # match non-fatal sequential behavior
        else:
            raw_questions, raw_layout = _do_exam_call()
            scheme_data = {"questions": []}

        # ---- Artifacts + callbacks (main thread, same order as before) ---

        # Save step-4 artifacts BEFORE on_exam_complete — the callback may raise
        # SystemExit(0) when --through 4 is used, so anything after it won't run.
        if artifact_dir is not None:
            try:
                _save_exam_questions(artifact_dir, raw_questions)
            except OSError:
                pass

        if on_exam_complete is not None:
            on_exam_complete(raw_questions)

        if isinstance(scheme_data.get("questions"), list):
            # Save step-5 artifacts before merging — preserves the raw scheme output.
            if artifact_dir is not None:
                try:
                    _save_mark_scheme(artifact_dir, scheme_data["questions"])
                except OSError:
                    pass
            # Notify caller that scheme parse is done, before merging.
            # The callback may raise SystemExit(0) for --through 5.
            if on_scheme_complete is not None:
                on_scheme_complete(scheme_data["questions"])

            # Suffix duplicate question numbers in exam questions so that
            # two questions both printed as "38" become "38" and "38_2".
            # Done after saving artifacts so 4_exam_questions.json retains original numbers.
            _seen_rq: dict[str, int] = {}
            for _node in raw_questions:
                _qnum = str(_node.get("number", ""))
                _seen_rq[_qnum] = _seen_rq.get(_qnum, 0) + 1
                if _seen_rq[_qnum] > 1:
                    _node["number"] = f"{_qnum}_{_seen_rq[_qnum]}"

            # Apply the same suffix to mark scheme entries so scheme_map keys align.
            # Done after saving 5_mark_scheme.json to preserve original numbers there.
            _seen_sq: dict[str, int] = {}
            for _sq in scheme_data["questions"]:
                if not isinstance(_sq, dict) or not _sq.get("number"):
                    continue
                _snum = _norm(_sq.get("number", ""))
                _seen_sq[_snum] = _seen_sq.get(_snum, 0) + 1
                if _seen_sq[_snum] > 1:
                    _sq["number"] = f"{_sq['number']}_{_seen_sq[_snum]}"

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

    layout = ExamLayout(
        rows=max(1, int(raw_layout.get("rows") or 1)),
        cols=max(1, int(raw_layout.get("cols") or 1)),
    )

    import logging as _logging
    valid_nodes = []
    for node in raw_questions:
        if not isinstance(node, dict) or "number" not in node:
            _logging.warning("ai_scaffold: skipping question node missing 'number' key: %r", node)
            continue
        valid_nodes.append(node)

    questions = [_json_to_question(node, layout) for node in valid_nodes]
    _fix_zero_mark_leaves(questions)
    return questions, layout


def _fix_zero_mark_leaves(questions: list) -> None:
    """Upgrade any leaf question with marks=0 but a marking criterion to marks=1.

    Gemini sometimes returns marks=0 for sub-questions whose mark allocation is
    not explicitly bracketed in the PDF. When a marking criterion exists the
    question is worth at least 1 mark.
    """
    import logging as _log
    for q in questions:
        if q.subquestions:
            _fix_zero_mark_leaves(q.subquestions)
        elif q.marks == 0 and q.marking_criteria:
            _log.warning(
                "ai_scaffold: %s has marks=0 but a marking criterion — upgraded to 1", q.number
            )
            q.marks = 1
