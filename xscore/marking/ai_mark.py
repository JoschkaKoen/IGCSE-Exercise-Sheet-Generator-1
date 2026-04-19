"""Step 12 — AI marking: iterate over student scan pages and produce Q-block text files.

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

from xscore.marking.blueprints import marked_to_md
from xscore.shared.exam_paths import artifact_blueprint_yaml_path, artifact_marked_path, artifact_marked_md_path, artifact_prompt_path, artifact_short_scaffold_yaml_path
from xscore.shared.prompt_logger import save_prompt
from xscore.shared.terminal_ui import format_duration, get_console, icon, warn_line


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
    extra_body: dict,
    prompt_save_path: Path | None = None,
) -> str:
    """Vision call to mark one scan page. Returns Q-block plain text.

    Retries up to 3 times with 2 s / 4 s backoff.
    Returns an empty string if all attempts fail.
    """
    layout = blueprint.get("layout") or {"rows": 1, "cols": 1}
    rows, cols = int(layout.get("rows", 1)), int(layout.get("cols", 1))
    criteria_text = _format_criteria(page_questions_info, rows=rows, cols=cols)

    system_prompt = (
        "You are an expert exam marker. You will be shown one page of a student's exam paper.\n"
        "For each question listed in the marking criteria, output a Q-block:\n\n"
        "Q<number>\n"
        "marks: <integer or decimal, between 0 and max_marks>\n"
        "answer: <student's answer>\n"
        "reasoning: <1 sentence — verdict and key reason>\n\n"
        "Output ONLY the Q-blocks. No JSON. No preamble. No trailing text.\n\n"
        "Rules:\n"
        "• student_answer for multiple_choice: report only the letter the student PHYSICALLY "
        "marked (written, circled, crossed, or ticked). Do NOT infer from question content or "
        "your own knowledge of correct answers. If no mark is visible, write '?'.\n"
        "• For calculation questions: transcribe the student's complete working and final answer.\n"
        "• For other question types: copy the student's written answer verbatim.\n"
        "• If handwriting is illegible: transcribe best attempt; mark unreadable words with [?].\n"
        "• assigned_marks: award 1 mark per satisfied criteria point, up to max_marks. "
        "For 'any N from' lists, each listed item is a separate mark point.\n"
        "• LaTeX math: wrap expressions containing ^, _, or math operators in $...$ "
        "(e.g. $10^{3}$, $v_0 = 5$ m/s, $\\frac{a}{b}$). "
        "Write \\% for percent signs, \\& for ampersands. "
        "Standard single-backslash LaTeX syntax is correct here — no escaping needed.\n"
        "• MCQ reminder: report only the letter the student physically marked — "
        "even if it appears to be a wrong answer. Do not use subject knowledge to guess."
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
            "Each question in the criteria carries subpage_row and subpage_col — use these "
            "coordinates to locate the student's answer. Do not confuse questions from "
            "different quadrants.\n"
            "The same question number may appear more than once (e.g. two Q38s in different "
            "sub-pages). Locate each by its subpage position and question_text."
        )

    q_list = ", ".join(
        re.sub(r"_\d+$", "", str(q.get("number", "?")))
        for q in (blueprint.get("questions") or [])
    )
    user_text = (
        f"Marking criteria:\n{criteria_text}\n\n"
        f"Output Q-blocks for: {q_list}"
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
        extra_body=extra_body,
    )

    save_prompt(prompt_save_path, model=model_id, messages=kwargs["messages"])

    try:
        resp = client.chat.completions.create(**kwargs)
        return (resp.choices[0].message.content or "").strip()
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Marking API error: {exc}")
        return ""


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


def _parse_text_response(text: str, blueprint: dict) -> dict:
    """Parse Q-block plain text into a dict matching the blueprint structure."""
    result: dict = {"student_name": "", "page": blueprint.get("page", 0), "questions": []}
    current_q: dict | None = None
    current_field: str | None = None

    bp_qs = blueprint.get("questions") or []

    def _flush():
        nonlocal current_q
        if current_q is not None:
            result["questions"].append(current_q)
            current_q = None

    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped.startswith("student:"):
            result["student_name"] = stripped[8:].strip()
            current_field = None
        elif stripped.startswith("page:"):
            try:
                result["page"] = int(stripped[5:].strip())
            except ValueError:
                pass
            current_field = None
        elif re.match(r"^Q\S+\s*$", stripped):
            _flush()
            qnum = stripped.strip()[1:]  # strip leading "Q"; blueprint stores "38" not "Q38"
            bp_q = next((q for q in bp_qs if q.get("number") == qnum), {})
            current_q = {
                **{k: v for k, v in bp_q.items() if k not in ("student_answer", "assigned_marks", "reasoning")},
                "number": qnum,
                "student_answer": "",
                "assigned_marks": None,
                "reasoning": "",
            }
            current_field = None
        elif current_q is not None:
            if stripped.startswith("marks:"):
                val = stripped[6:].strip()
                try:
                    current_q["assigned_marks"] = float(val) if "." in val else int(val)
                except ValueError:
                    current_q["assigned_marks"] = None
                current_field = "marks"
            elif stripped.startswith("answer:"):
                current_q["student_answer"] = stripped[7:].strip()
                current_field = "student_answer"
            elif stripped.startswith("reasoning:"):
                current_q["reasoning"] = stripped[10:].strip()
                current_field = "reasoning"
            elif stripped and current_field in ("student_answer", "reasoning"):
                current_q[current_field] += " " + stripped.strip()
    _flush()
    return result


def _dict_to_marked_txt(filled: dict) -> str:
    """Serialise a filled marking dict to Q-block plain text."""
    lines = [
        f"student: {filled.get('student_name', '')}",
        f"page: {filled.get('page', '')}",
        "",
    ]
    for q in filled.get("questions") or []:
        lines.append(f"Q{q.get('number', '?')}")
        marks = q.get("assigned_marks")
        lines.append(f"marks: {'' if marks is None else marks}")
        lines.append(f"answer: {q.get('student_answer', '')}")
        lines.append(f"reasoning: {q.get('reasoning', '')}")
        lines.append("")
    return "\n".join(lines)


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

    from eXercise.ai_client import make_ai_client
    from xscore.shared.exam_paths import artifact_exam_student_list_json_path

    result = make_ai_client(model_env="MARKING_MODEL", default_model="qwen3.6-plus, off")
    if result is None:
        raise RuntimeError(
            "MARKING_MODEL client could not be created — check DASHSCOPE_API_KEY in .env"
        )
    client, model_id, _provider, _effort = result
    extra_body = {"enable_thinking": False} if _provider in ("qwen", "openai", "xai") else {}

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
    import yaml as _yaml
    short_report_path = artifact_short_scaffold_yaml_path(ctx.artifact_dir)
    short_report = _yaml.safe_load(short_report_path.read_text(encoding="utf-8"))
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

    def _mark_student(assignment: dict) -> list[dict]:
        """Mark one student's pages using scan-detected page assignments.

        Opens its own fitz handle (fitz is not thread-safe).
        """
        student_name: str = assignment["student_name"]
        page_numbers: list[int] = assignment["page_numbers"]  # 1-based scan pages
        import yaml
        local_timings: list[dict] = []
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
                blueprint = yaml.safe_load(
                    artifact_blueprint_yaml_path(ctx.artifact_dir, p_label).read_text(
                        encoding="utf-8"
                    )
                )
                safe_name = student_name or f"Unknown_{scan_page}"

                t0 = time.perf_counter()
                raw_text = _mark_page(
                    client, model_id, b64, blueprint,
                    page_questions.get(p_label, []), extra_body,
                    prompt_save_path=artifact_prompt_path(
                        ctx.artifact_dir, f"12_marked_{safe_name}_{p_label}"
                    ),
                )
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

                filled = _parse_text_response(raw_text, blueprint)
                filled["student_name"] = student_name
                _fix_mc_marks(filled, page_questions.get(p_label, []))

                out_txt = artifact_marked_path(ctx.artifact_dir, safe_name, p_label)
                out_txt.parent.mkdir(parents=True, exist_ok=True)
                out_txt.write_text(_dict_to_marked_txt(filled), encoding="utf-8")
                artifact_marked_md_path(ctx.artifact_dir, safe_name, p_label).write_text(
                    marked_to_md(filled), encoding="utf-8"
                )
        finally:
            doc.close()
        return local_timings

    _live_ctx = Live("", console=get_console(), refresh_per_second=4) if _use_live else contextlib.nullcontext()
    with _live_ctx as live:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_mark_student, a): a["student_name"] for a in raw_assignments
            }
            for fut in as_completed(futures):
                with timings_lock:
                    api_call_timings.extend(fut.result())

    return api_call_timings
