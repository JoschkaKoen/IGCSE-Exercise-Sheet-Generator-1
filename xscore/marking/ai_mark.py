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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from xscore.marking.blueprints import marked_to_md
from xscore.marking.kimi_helpers import parse_json_safe
from xscore.shared.exam_paths import artifact_blueprint_json_path, artifact_marked_json_path, artifact_marked_md_path, artifact_prompt_path, artifact_short_scaffold_json_path
from xscore.shared.prompt_logger import save_prompt
from xscore.shared.terminal_ui import api_latency_line, info_line, warn_line


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
        "Fill in the provided JSON template by reading the student's answers and applying the "
        "marking criteria below. Return ONLY valid JSON in the exact same schema — do not add "
        "or remove keys. Use proper JSON escape sequences in all strings (\\n for newlines, "
        "\\t for tabs) — never embed literal control characters. For each question:\n"
        "  • student_answer: what the student wrote. For multiple_choice: find the option the student "
        "physically marked (written letter, circled letter, cross, or tick) and report that single "
        "letter. Do NOT infer from the question content or your own knowledge of which answer is correct. "
        "If no mark is visible, report '?'.\n"
        "  • assigned_marks: an integer between 0 and max_marks\n"
        "  • reasoning: a brief justification for the marks awarded\n"
        "IMPORTANT — LaTeX formatting: any expression containing ^, _, or math operators MUST "
        "be wrapped in $...$ (e.g. write \"$10^{3}$\", \"$v_0 = 5$ m/s\", never \"10^3\" or "
        "\"v_0 = 5 m/s\"). Also write \\% for percent signs, \\& for ampersands. Failing to "
        "use math mode for such expressions will crash the PDF renderer. "
        "All LaTeX commands in JSON strings MUST use a double backslash — "
        "e.g. write $\\\\rightarrow$, $\\\\times$, $\\\\approx$ — never $\\rightarrow$. "
        "A single \\r, \\t, or \\n before a command name is a JSON control character "
        "that corrupts the output."
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
            "IMPORTANT — question number matching: template question numbers may not match the "
            "printed number on the image — e.g. two questions both printed as '38' "
            "appear as '38' and '38_2' in the template. Locate each question using "
            "subpage_row, subpage_col, and question_text. Do not reproduce question_text or "
            "answer_options in your response — fill in only student_answer, assigned_marks, "
            "and reasoning."
        )
    system_prompt += (
        "\nMCQ reminder: report only the letter the student physically marked — "
        "even if it appears to be a wrong answer. Do not use subject knowledge to guess."
    )
    user_text = (
        f"Marking criteria:\n{criteria_text}\n\n"
        f"Blueprint template to fill in:\n{blueprint_json}"
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
        response_format={"type": "json_object"},
        extra_body=extra_body,
    )

    save_prompt(prompt_save_path, model=model_id, messages=kwargs["messages"])

    for attempt in range(1, 4):
        try:
            t0 = time.perf_counter()
            resp = client.chat.completions.create(**kwargs)
            api_latency_line(time.perf_counter() - t0, label="marking")
            raw = resp.choices[0].message.content or ""
            result = parse_json_safe(raw)
            if result is not None:
                _fix_mc_marks(result, page_questions_info)
                return result
            warn_line(f"Marking call returned unparseable JSON (attempt {attempt}/3) — retrying")
        except Exception as exc:  # noqa: BLE001
            warn_line(f"Marking API error (attempt {attempt}/3): {exc}")
            if attempt < 3:
                time.sleep(2**attempt)

    warn_line("All marking attempts failed — using blank blueprint")
    return blueprint.copy()


def _fix_mc_marks(result: dict, page_questions_info: list[dict]) -> None:
    """Normalise student_answer and recompute assigned_marks for MCQ questions in-place.

    The AI is not shown the correct answer for MCQs, so it cannot award marks
    reliably. This function overrides assigned_marks deterministically and
    normalises the extracted letter (e.g. "b." → "B").
    """
    mc_correct: dict[str, str] = {
        q.get("number", ""): (q.get("correct_answer") or "").strip().upper()
        for q in page_questions_info
        if q.get("question_type") == "multiple_choice" and q.get("correct_answer")
    }
    if not mc_correct:
        return
    for q in result.get("questions", []):
        num = q.get("number", "")
        if num not in mc_correct:
            continue
        raw_ans = (q.get("student_answer") or "").strip()
        student_ans = raw_ans[0].upper() if raw_ans and raw_ans[0].isalpha() else "?"
        q["student_answer"] = student_ans
        max_m = int(q.get("max_marks") or 1)
        q["assigned_marks"] = max_m if student_ans == mc_correct[num] else 0


def _format_criteria(questions_info: list[dict], *, rows: int = 1, cols: int = 1) -> str:
    """Format question marking criteria for the AI prompt."""
    if not questions_info:
        return "(no questions assigned to this page)"
    multi_subpage = rows > 1 or cols > 1
    parts = []
    for q in questions_info:
        line = f"Q{q.get('number', '?')} [{q.get('question_type', '')}] — {q.get('marks', '?')} mark(s)"
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
            line += f"\n  Criteria: {q['marking_criteria']}"
        parts.append(line)
    return "\n".join(parts)


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

    def _mark_student(assignment: dict) -> list[dict]:
        """Mark one student's pages using scan-detected page assignments.

        Opens its own fitz handle (fitz is not thread-safe).
        """
        student_name: str = assignment["student_name"]
        page_numbers: list[int] = assignment["page_numbers"]  # 1-based scan pages
        local_timings: list[dict] = []
        doc = fitz.open(str(ctx.cleaned_pdf))
        try:
            for p_label, scan_page in enumerate(page_numbers, 1):
                scan_idx = scan_page - 1  # fitz is 0-based
                info_line(
                    f"Student '{student_name}' · page {p_label}/{len(page_numbers)}"
                )

                b64 = _render_page_b64(doc, scan_idx, dpi=dpi)
                blueprint = json.loads(
                    artifact_blueprint_json_path(ctx.artifact_dir, p_label).read_text(
                        encoding="utf-8"
                    )
                )
                safe_name = student_name or f"Unknown_{scan_page}"

                t0 = time.perf_counter()
                filled = _mark_page(
                    client, model_id, b64, blueprint,
                    page_questions.get(p_label, []), extra_body,
                    prompt_save_path=artifact_prompt_path(
                        ctx.artifact_dir, f"12_marked_{safe_name}_{p_label}"
                    ),
                )
                mark_dur = round(time.perf_counter() - t0, 2)
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
        return local_timings

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_mark_student, a): a["student_name"] for a in raw_assignments
        }
        for fut in as_completed(futures):
            with timings_lock:
                api_call_timings.extend(fut.result())

    return api_call_timings
