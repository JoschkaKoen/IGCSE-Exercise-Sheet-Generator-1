"""Step 12 — AI marking: iterate over student scan pages and fill blueprint JSONs.

Uses the MARKING_MODEL env var (default: qwen3.6-plus, off) via make_ai_client().
Requires DASHSCOPE_API_KEY to be set in .env.

Students are processed in parallel (MARKING_WORKERS workers, default 4).
Each worker opens its own fitz document handle (fitz is not thread-safe).
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from eXercise.ai_client import collect_streamed_response
from xscore.marking.blueprints import marked_to_md
from xscore.shared.exam_paths import artifact_blueprint_json_path, artifact_marked_failed_path, artifact_marked_json_path, artifact_marked_md_path, artifact_prompt_path, artifact_short_scaffold_json_path
from xscore.shared.prompt_logger import save_prompt
from xscore.shared.terminal_ui import format_duration, get_console, icon, warn_line


_DEFAULT_MARKING_MODEL = "qwen3.6-plus, off"


class MarkingFailure(Exception):
    """Raised when all retry attempts to mark a page are exhausted."""
    def __init__(self, *, attempts: int, last_exc: BaseException) -> None:
        super().__init__(f"All {attempts} marking attempts failed: {last_exc}")
        self.attempts = attempts
        self.last_exc = last_exc


class _FillQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    number: str
    subpage_row: int
    subpage_col: int
    student_answer: str
    assigned_marks: int
    reasoning: str


class _FillResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    questions: list[_FillQuestion]


_FILL_RESPONSE_FORMAT: dict = {
    "type": "json_schema",
    "json_schema": {
        "name": "FillResponse",
        "schema": _FillResponse.model_json_schema(),
        "strict": True,
    },
}


def _fix_latex_in_math(text: str) -> str:
    """Normalise AI LaTeX inside $...$ blocks before storing to disk.

    Three passes, math blocks only:
    1. Restore JSON-escape control chars (\\t → \\t, etc.) — any number of leading backslashes.
    2. Collapse double-escaped \\\\letter → \\letter (AI over-escaping in JSON).
    3. Strip \\text{} wrapper around bare math commands (\\text{\\pi} → \\pi).
    """
    parts = re.split(r'(\$[^$]+\$)', text)
    out = []
    for part in parts:
        if part.startswith('$') and part.endswith('$') and len(part) > 1:
            for ctrl, letter in [('\t', 't'), ('\f', 'f'), ('\r', 'r'), ('\b', 'b')]:
                part = re.sub(r'\\*' + re.escape(ctrl),
                              lambda m, _l=letter: '\\' + _l, part)
            part = re.sub(r'\\\\([a-zA-Z])', r'\\\1', part)
            part = re.sub(r'\\text\{(\\[a-zA-Z]+)\}', r'\1', part)
        out.append(part)
    return ''.join(out)


def _render_page_b64(doc: Any, page_idx: int, dpi: int = 150) -> str:
    """Render a fitz Document page at *page_idx* as base64 JPEG.

    The document must be already open; the caller owns its lifecycle.
    """
    import numpy as np
    from PIL import Image

    import fitz
    from xscore.extraction.images import to_jpeg_bytes

    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = doc[page_idx].get_pixmap(matrix=mat, colorspace=fitz.csRGB)
    img = Image.fromarray(
        np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
    )
    return base64.b64encode(to_jpeg_bytes(img, quality=90)).decode()



def _quadrant_label(row: int, col: int, total_rows: int, total_cols: int) -> str:
    v = "top" if row == 1 else "bottom" if row == total_rows else f"row {row}"
    h = "left" if col == 1 else "right" if col == total_cols else f"col {col}"
    return f"{v}-{h}"


def _mark_page(
    client: Any,
    model_id: str,
    b64: str,
    blueprint: dict,
    page_questions_info: list[dict],
    thinking_kw: dict,
    use_stream: bool = False,
    prompt_save_path: Path | None = None,
) -> dict:
    """Vision call to fill in a marking blueprint for one scan page.

    Retries up to 3 times with 2 s / 4 s backoff (same pattern as kimi_helpers).
    Returns the original blueprint (all blanks) if all attempts fail.
    """
    layout = blueprint.get("layout") or {"rows": 1, "cols": 1}
    rows, cols = int(layout.get("rows", 1)), int(layout.get("cols", 1))
    criteria_text = _format_criteria(page_questions_info, rows=rows, cols=cols)
    blueprint_json = json.dumps(blueprint, indent=2, ensure_ascii=False)

    system_prompt = (
        "You are an expert exam marker. You will be shown one page of a student's exam paper. "
        "Read the question list below and the marking criteria, then return a JSON object with a "
        "'questions' array — one entry per question in the list, in the same order. "
        "Each entry must have: number (copy from the list), subpage_row (copy from the list), "
        "subpage_col (copy from the list), student_answer (string), assigned_marks (integer 0–max_marks), "
        "reasoning (string, 1–2 sentences). "
        "Do not include question_text, answer_options, or any other fields.\n"
        "For each question:\n"
        "  • student_answer: what the student wrote. For multiple_choice: find the option the student "
        "physically marked (written letter, circled letter, cross, or tick) and report that single "
        "letter. Do NOT infer from the question content or your own knowledge of which answer is correct. "
        "If no mark is visible, report '?'. "
        "For calculation questions transcribe the student's complete working and final answer. "
        "For all other question types copy the student's written answer verbatim. "
        "If handwriting is illegible, transcribe your best attempt and mark unreadable words with [?].\n"
        "  • assigned_marks: an integer between 0 and max_marks. "
        "Award 1 mark for each criteria point the student satisfies, up to max_marks. "
        "For 'any N from' lists, each listed item is a separate mark point.\n"
        "  • reasoning: 1–2 sentences maximum — state the verdict and the key reason. Do NOT show calculations, working-out steps, or deliberation.\n"
        "IMPORTANT — LaTeX formatting: any expression containing ^, _, or math operators MUST "
        "be wrapped in $...$ (e.g. write \"$10^{3}$\", \"$v_0 = 5$ m/s\", never \"10^3\" or "
        "\"v_0 = 5 m/s\"). Also write \\% for percent signs, \\& for ampersands. "
        "Use LaTeX commands for math symbols — write \\times, \\approx, \\rightarrow, "
        "\\leq, \\geq, \\pm, \\infty, \\pi, \\theta, \\therefore, etc. — "
        "never Unicode characters (×, ≈, →, ≤, ≥, ±, ∞, π, θ, ∴). "
        "Never use \\text{} around a math symbol — \\text{} is only for prose/units "
        "(e.g. \\text{ m/s}). Write \\pi directly, not \\text{\\pi}. "
        "Use a single backslash for every LaTeX command — write \\times, not \\\\times. "
        "Failing to use math mode or using Unicode symbols will crash the PDF renderer."
    )
    if rows > 1 or cols > 1:
        grid_desc = "\n".join(
            f"  row {r} col {c} = {_quadrant_label(r, c, rows, cols)}"
            for r in range(1, rows + 1)
            for c in range(1, cols + 1)
        )
        system_prompt += (
            f"\n\nThis exam page has a {rows}×{cols} grid of sub-pages (row-major reading order):\n"
            f"{grid_desc}\n"
            "Each question in the template carries subpage_row and subpage_col that tell you "
            "which quadrant it lives in. Use these coordinates to locate the student's answer "
            "— do not confuse questions from different quadrants with each other.\n"
            "IMPORTANT — question number matching: the same question number may appear "
            "more than once in the template (e.g. two questions both numbered '38' in "
            "different sub-pages). Locate each occurrence using subpage_row, subpage_col, "
            "and question_text — do not rely on the number field alone to find questions "
            "on the paper. "
            "The same question number may appear more than once in the same sub-page — "
            "locate each occurrence by its question_text. "
            "Do not reproduce question_text or "
            "answer_options in your response — fill in only student_answer, assigned_marks, "
            "and reasoning."
        )
    system_prompt += (
        "\nMCQ reminder: report only the letter the student physically marked — "
        "even if it appears to be a wrong answer. Do not use subject knowledge to guess."
    )
    user_text = (
        f"Marking criteria:\n{criteria_text}\n\n"
        f"Question list (read to identify questions — return one entry per question):\n{blueprint_json}"
    )
    kwargs: dict[str, Any] = dict(
        model=model_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            },
        ],
        response_format=_FILL_RESPONSE_FORMAT,
    )
    kwargs.update(thinking_kw)

    save_prompt(prompt_save_path, model=model_id, messages=kwargs["messages"])

    _last_exc: BaseException = RuntimeError("no attempts made")
    for attempt in range(1, 4):
        try:
            if use_stream:
                stream = client.chat.completions.create(**kwargs, stream=True)
                raw = collect_streamed_response(stream)
            else:
                resp = client.chat.completions.create(**kwargs)
                raw = resp.choices[0].message.content or ""
            try:
                fill = _FillResponse.model_validate_json(raw)
            except ValidationError as ve:
                warn_line(f"Marking call schema validation failed (attempt {attempt}/3) — retrying")
                _last_exc = ve
                continue
            for fq in fill.questions:
                fq.reasoning = _fix_latex_in_math(fq.reasoning)
                fq.student_answer = _fix_latex_in_math(fq.student_answer)
            result = blueprint.copy()
            fill_map = {
                (q.number, q.subpage_row, q.subpage_col): q for q in fill.questions
            }
            for bq in result.get("questions", []):
                _row = bq.get("subpage_row")
                _col = bq.get("subpage_col")
                key = (
                    str(bq.get("number", "")),
                    int(_row) if _row is not None else 1,
                    int(_col) if _col is not None else 1,
                )
                if key in fill_map:
                    fq = fill_map[key]
                    bq["student_answer"] = fq.student_answer
                    bq["assigned_marks"] = fq.assigned_marks
                    bq["reasoning"] = fq.reasoning
            _blueprint_keys = {
                (
                    str(bq.get("number", "")),
                    int(bq.get("subpage_row")) if bq.get("subpage_row") is not None else 1,
                    int(bq.get("subpage_col")) if bq.get("subpage_col") is not None else 1,
                )
                for bq in result.get("questions", [])
            }
            _unmatched = [
                q for q in fill.questions
                if (q.number, q.subpage_row, q.subpage_col) not in _blueprint_keys
            ]
            if _unmatched:
                warn_line(
                    f"Marking: {len(_unmatched)} AI entr{'y' if len(_unmatched) == 1 else 'ies'} "
                    f"didn't match blueprint: "
                    f"{[(q.number, q.subpage_row, q.subpage_col) for q in _unmatched]}"
                )
            _fill_keys = {(q.number, q.subpage_row, q.subpage_col) for q in fill.questions}
            _unfilled = [
                bq.get("number") for bq in result.get("questions", [])
                if (
                    str(bq.get("number", "")),
                    int(bq.get("subpage_row")) if bq.get("subpage_row") is not None else 1,
                    int(bq.get("subpage_col")) if bq.get("subpage_col") is not None else 1,
                ) not in _fill_keys
            ]
            if _unfilled:
                warn_line(f"Marking: {len(_unfilled)} blueprint question(s) skipped by AI: {_unfilled}")
            _fix_mc_marks(result, page_questions_info)
            for bq in result.get("questions", []):
                max_m = bq.get("max_marks")
                if max_m is None:
                    continue
                m = bq.get("assigned_marks", 0)
                if not isinstance(m, int) or m < 0 or m > int(max_m):
                    warn_line(
                        f"Marking: Q{bq.get('number')} assigned_marks={m} out of range "
                        f"[0, {max_m}] — clamping"
                    )
                    bq["assigned_marks"] = max(0, min(int(m) if isinstance(m, (int, float)) else 0, int(max_m)))
            return result
        except Exception as exc:  # noqa: BLE001
            warn_line(f"Marking API error (attempt {attempt}/3): {exc}")
            _last_exc = exc
            if attempt < 3:
                time.sleep(2**attempt)

    raise MarkingFailure(attempts=3, last_exc=_last_exc)


def _fix_mc_marks(result: dict, page_questions_info: list[dict]) -> None:
    """Normalise student_answer and recompute assigned_marks for MCQ questions in-place.

    The AI is not shown the correct answer for MCQs, so it cannot award marks
    reliably. This function overrides assigned_marks deterministically and
    normalises the extracted letter (e.g. "b." → "B").

    Keyed by question_text (not number) because duplicate question numbers
    (e.g. two Q38s on the same page) share the same stripped number after
    _2 is removed from blueprints.
    """
    mc_correct: dict[str, str] = {
        (q.get("question_text") or q.get("text") or "").strip(): (q.get("correct_answer") or "").strip().upper()
        for q in page_questions_info
        if q.get("question_type") == "multiple_choice" and q.get("correct_answer")
    }
    if not mc_correct:
        return
    for q in result.get("questions", []):
        qt = (q.get("question_text") or "").strip()
        if qt not in mc_correct:
            continue
        raw_ans = (q.get("student_answer") or "").strip()
        student_ans = raw_ans[0].upper() if raw_ans and raw_ans[0].isalpha() else "?"
        q["student_answer"] = student_ans
        max_m = int(q.get("max_marks") or 1)
        q["assigned_marks"] = max_m if student_ans == mc_correct[qt] else 0


def _clean_criteria_line(l: str) -> str:
    c = l.lstrip().removeprefix("[None]").lstrip(" ")
    return "  " + c[2:] if c.startswith("\\t") else c


def _format_criteria(questions_info: list[dict], *, rows: int = 1, cols: int = 1) -> str:
    """Format question marking criteria for the AI prompt."""
    if not questions_info:
        return "(no questions assigned to this page)"
    multi_subpage = rows > 1 or cols > 1
    # Sort by quadrant so same-subpage questions are grouped together.
    sorted_qs = sorted(
        questions_info,
        key=lambda q: (int(q.get("subpage_row") or 1), int(q.get("subpage_col") or 1)),
    )
    parts = []
    for q in sorted_qs:
        display_num = re.sub(r"_\d+$", "", str(q.get("number", "?")))
        line = f"Q{display_num} [{q.get('question_type', '')}] — {q.get('marks', '?')} mark(s)"
        if multi_subpage:
            r = int(q.get("subpage_row") or 1)
            c = int(q.get("subpage_col") or 1)
            label = _quadrant_label(r, c, rows, cols)
            line += f"  (sub-page row {r}, col {c} — {label})"
        question_text = (q.get("text") or q.get("question_text") or "").strip()
        if question_text:
            line += f"\n  Question: \"{question_text}\""
        answer_options = q.get("answer_options") or []
        if answer_options:
            opts_lines = "\n".join(
                f"    {o.get('letter', '?')}) {o.get('text', '')}"
                for o in answer_options
                if isinstance(o, dict)
            )
            line += f"\n  Options:\n{opts_lines}"
        if q.get("correct_answer") and q.get("question_type") != "multiple_choice":
            line += f"\n  Correct answer: {q['correct_answer']}"
        if q.get("marking_criteria"):
            lines = [_clean_criteria_line(l) for l in q["marking_criteria"].splitlines()]
            if len(lines) > 1:
                bulleted = []
                for l in lines:
                    stripped = l.lstrip(" ")
                    if stripped and not stripped.startswith("•") and not stripped.endswith(":"):
                        bulleted.append(l[: len(l) - len(stripped)] + "• " + stripped)
                    else:
                        bulleted.append(l)
                lines = bulleted
            cleaned = "\n".join(lines)
            line += f"\n  Criteria: {cleaned}"
        parts.append(line)
    return "\n\n".join(parts)


def _flatten_leaf_questions(questions: list[dict]) -> list[dict]:
    """Depth-first flatten of question tree to leaf nodes only."""
    result = []
    for q in questions:
        subs = q.get("subquestions") or []
        if subs:
            result.extend(_flatten_leaf_questions(subs))
        else:
            result.append(q)
    return result


def run_ai_marking(ctx: Any, *, dpi: int | None = None) -> list[dict]:
    """Run the full AI marking loop for all students and pages.

    Reads page assignments from ``10_exam_student_list.json`` (written by step 10)
    so each student's scan pages are determined by name detection, not position.
    Students are processed in parallel (MARKING_WORKERS env var, default varies with cpu_count).
    *dpi* defaults to ``MARKING_DPI`` when not supplied.
    Returns a list of API call timing records for step 14.
    """
    from xscore.config import MARKING_DPI
    if dpi is None:
        dpi = MARKING_DPI

    import fitz

    from eXercise.ai_client import make_ai_client, build_thinking_kwargs
    from xscore.shared.exam_paths import artifact_exam_student_list_json_path

    result = make_ai_client(model_env="MARKING_MODEL", default_model=_DEFAULT_MARKING_MODEL)
    if result is None:
        raise RuntimeError(
            "MARKING_MODEL client could not be created — check DASHSCOPE_API_KEY in .env"
        )
    client, model_id, _provider, _effort = result
    _use_stream, _thinking_kw = build_thinking_kwargs(_provider, _effort)

    # Load page assignments produced by step 10 name detection.
    list_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
    if not list_path.exists():
        raise FileNotFoundError(
            f"10_exam_student_list.json not found at {list_path} — run step 10 first"
        )
    raw_assignments: list[dict] = json.loads(list_path.read_text(encoding="utf-8"))
    # Each entry: {"student_name": str, "page_numbers": [int, ...], "confidence": str}

    _instr = getattr(ctx, "instruction", None)
    if _instr is not None:
        sf = _instr.student_filter
        if sf.mode == "specific" and sf.names:
            raw_assignments = [a for a in raw_assignments if a["student_name"] in sf.names]
        elif sf.mode == "first_n" and sf.n:
            raw_assignments = raw_assignments[: sf.n]

    # Load short report once; build page → leaf-questions lookup (read-only, shared)
    short_report_path = artifact_short_scaffold_json_path(ctx.artifact_dir)
    short_report = json.loads(short_report_path.read_text(encoding="utf-8"))
    all_leaf_qs = _flatten_leaf_questions(short_report.get("questions", []))
    page_questions: dict[int, list[dict]] = {}
    for q in all_leaf_qs:
        pg = int(q.get("page") or 0)
        page_questions.setdefault(pg, []).append(q)

    workers = int(os.environ.get("MARKING_WORKERS", str(min(os.cpu_count() or 4, 16))))
    timings_lock = threading.Lock()
    api_call_timings: list[dict] = []

    import contextlib
    import sys
    from rich.live import Live

    _use_live = sys.stdout.isatty()
    _display_lock = threading.Lock()
    _student_lines: dict[str, str] = {}

    def _render() -> str:  # caller must hold _display_lock
        return "\n".join(_student_lines.values()) if _student_lines else ""

    def _mark_student(assignment: dict) -> tuple[list[dict], list[dict]]:
        """Mark one student's pages using scan-detected page assignments.

        Opens its own fitz handle (fitz is not thread-safe).
        Returns (timings, failures) where failures is non-empty if any page exhausted all retries.
        """
        student_name: str = assignment["student_name"]
        page_numbers: list[int] = assignment["page_numbers"]  # 1-based scan pages
        local_timings: list[dict] = []
        local_failures: list[dict] = []
        doc = fitz.open(str(ctx.cleaned_pdf))
        try:
            for p_label, scan_page in enumerate(page_numbers, 1):
                scan_idx = scan_page - 1  # fitz is 0-based
                key = f"{student_name}_{p_label}"
                with _display_lock:
                    _student_lines[key] = f"[dim]  {icon('info')}  Student '{student_name}' · page {p_label}/{len(page_numbers)}[/]"
                    if _use_live:
                        live.update(_render())

                b64 = _render_page_b64(doc, scan_idx, dpi=dpi)
                blueprint = json.loads(
                    artifact_blueprint_json_path(ctx.artifact_dir, p_label).read_text(
                        encoding="utf-8"
                    )
                )
                safe_name = student_name or f"Unknown_{scan_page}"

                t0 = time.perf_counter()
                try:
                    filled = _mark_page(
                        client, model_id, b64, blueprint,
                        page_questions.get(p_label, []), _thinking_kw,
                        use_stream=_use_stream,
                        prompt_save_path=artifact_prompt_path(
                            ctx.artifact_dir, f"12_marked_{safe_name}_{p_label}"
                        ),
                    )
                except MarkingFailure as mf:
                    warn_line(
                        f"Marking failed: student '{student_name}' page {p_label} — {mf.last_exc}"
                    )
                    filled = blueprint.copy()
                    filled["student_name"] = student_name
                    local_failures.append({
                        "student": student_name,
                        "page": p_label,
                        "attempts": mf.attempts,
                        "error": str(mf.last_exc),
                    })
                    out_json = artifact_marked_json_path(ctx.artifact_dir, safe_name, p_label)
                    out_json.parent.mkdir(parents=True, exist_ok=True)
                    out_json.write_text(
                        json.dumps(filled, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                    artifact_marked_md_path(ctx.artifact_dir, safe_name, p_label).write_text(
                        marked_to_md(filled), encoding="utf-8"
                    )
                    failed_path = artifact_marked_failed_path(ctx.artifact_dir, safe_name, p_label)
                    failed_path.parent.mkdir(parents=True, exist_ok=True)
                    failed_path.write_text(
                        json.dumps(local_failures[-1], indent=2, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    with _display_lock:
                        _student_lines[key] = (
                            f"[red]  {icon('warn')}  Student '{student_name}' · page {p_label}/{len(page_numbers)}"
                            f"  ·  FAILED[/]"
                        )
                        if _use_live:
                            live.update(_render())
                    continue

                mark_dur = round(time.perf_counter() - t0, 2)
                with _display_lock:
                    _student_lines[key] = (
                        f"[dim]  {icon('info')}  Student '{student_name}' · page {p_label}/{len(page_numbers)}"
                        f"  ·  {format_duration(mark_dur)}[/]"
                    )
                    if _use_live:
                        live.update(_render())
                local_timings.append({
                    "phase": "marking",
                    "student": student_name,
                    "page": p_label,
                    "duration_s": mark_dur,
                })

                filled["student_name"] = student_name
                out_json = artifact_marked_json_path(ctx.artifact_dir, safe_name, p_label)
                out_json.parent.mkdir(parents=True, exist_ok=True)
                out_json.write_text(
                    json.dumps(filled, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                artifact_marked_md_path(ctx.artifact_dir, safe_name, p_label).write_text(
                    marked_to_md(filled), encoding="utf-8"
                )
        finally:
            doc.close()
        return local_timings, local_failures

    all_failures: list[dict] = []
    _live_ctx = Live("", console=get_console(), refresh_per_second=4) if _use_live else contextlib.nullcontext()
    with _live_ctx as live:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_mark_student, a): a["student_name"] for a in raw_assignments
            }
            for fut in as_completed(futures):
                timings, failures = fut.result()
                with timings_lock:
                    api_call_timings.extend(timings)
                    all_failures.extend(failures)

    ctx.marking_failures = all_failures
    return api_call_timings
