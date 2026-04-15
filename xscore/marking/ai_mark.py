"""Step 12 — AI marking: iterate over student scan pages and fill blueprint JSONs.

Uses the MARKING_MODEL env var (default: qwen3.6-plus, off) via make_ai_client().
Requires DASHSCOPE_API_KEY to be set in .env.
"""

from __future__ import annotations

import base64
import difflib
import json
import time
from pathlib import Path
from typing import Any

from xscore.marking.kimi_helpers import parse_json_safe
from xscore.shared.exam_paths import artifact_blueprint_json_path, artifact_marked_json_path
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


def _identify_student(
    client: Any,
    model_id: str,
    b64: str,
    roster: list[str],
    fallback_idx: int,
    extra_body: dict,
) -> str:
    """Vision call to read the student name from the top of the first exam page.

    Fuzzy-matches the AI response against the roster.  Falls back to
    ``Unknown_<idx>`` if the call fails or no match is found.
    """
    roster_json = json.dumps(roster, ensure_ascii=False)
    messages = [
        {
            "role": "system",
            "content": (
                "You are identifying which student's exam this is. "
                "Return ONLY valid JSON: {\"student_name\": \"<name from the list>\"}."
            ),
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Student roster (choose exactly one name):\n{roster_json}\n\n"
                        "Look at the top third of this exam page to find the student's handwritten "
                        "name. Return the roster name that best matches."
                    ),
                },
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        },
    ]
    try:
        resp = client.chat.completions.create(
            model=model_id,
            messages=messages,
            response_format={"type": "json_object"},
            extra_body=extra_body,
        )
        raw = resp.choices[0].message.content or ""
        data = parse_json_safe(raw)
        raw_name = str(data.get("student_name", "")).strip()
        if raw_name:
            matches = difflib.get_close_matches(raw_name, roster, n=1, cutoff=0.6)
            if matches:
                return matches[0]
            warn_line(f"Name '{raw_name}' not in roster — using as-is")
            return raw_name
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Student ID call failed: {exc}")
    return f"Unknown_{fallback_idx}"


def _mark_page(
    client: Any,
    model_id: str,
    b64: str,
    blueprint: dict,
    page_questions_info: list[dict],
    extra_body: dict,
) -> dict:
    """Vision call to fill in a marking blueprint for one scan page.

    Returns the filled blueprint dict (same schema with student_answer,
    assigned_marks, reasoning populated).  Falls back to the original
    blueprint (all blanks) if the call fails or returns invalid JSON.
    """
    criteria_text = _format_criteria(page_questions_info)
    blueprint_json = json.dumps(blueprint, indent=2, ensure_ascii=False)

    system_prompt = (
        "You are an expert exam marker. You will be shown one page of a student's exam paper. "
        "Fill in the provided JSON template by reading the student's answers and applying the "
        "marking criteria below. Return ONLY valid JSON in the exact same schema — do not add "
        "or remove keys. For each question:\n"
        "  • student_answer: what the student wrote (for multiple_choice: a single letter A–D)\n"
        "  • assigned_marks: an integer between 0 and max_marks\n"
        "  • reasoning: a brief justification for the marks awarded"
    )
    user_text = (
        f"Marking criteria:\n{criteria_text}\n\n"
        f"Blueprint template to fill in:\n{blueprint_json}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        },
    ]
    try:
        resp = client.chat.completions.create(
            model=model_id,
            messages=messages,
            response_format={"type": "json_object"},
            extra_body=extra_body,
        )
        raw = resp.choices[0].message.content or ""
        result = parse_json_safe(raw)
        if result:
            return result
        warn_line("Marking call returned empty/invalid JSON — using blank blueprint")
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Marking call failed: {exc}")

    return blueprint.copy()


def _format_criteria(questions_info: list[dict]) -> str:
    """Format question marking criteria for the AI prompt."""
    if not questions_info:
        return "(no questions assigned to this page)"
    parts = []
    for q in questions_info:
        line = f"Q{q.get('number', '?')} [{q.get('question_type', '')}] — {q.get('marks', '?')} mark(s)"
        if q.get("correct_answer"):
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


def run_ai_marking(ctx: Any, *, dpi: int = 150) -> list[dict]:
    """Run the full AI marking loop for all students and pages.

    Returns a list of API call timing records (one per call) for step 14.
    """
    import fitz

    from eXercise.ai_client import make_ai_client

    result = make_ai_client(model_env="MARKING_MODEL", default_model="qwen3.6-plus, off")
    if result is None:
        raise RuntimeError(
            "MARKING_MODEL client could not be created — check DASHSCOPE_API_KEY in .env"
        )
    client, model_id, _provider, _effort = result
    extra_body = {"enable_thinking": False}

    # Load short report once; build page → leaf-questions lookup
    short_report_path = ctx.artifact_dir / "6_short_report.json"
    short_report = json.loads(short_report_path.read_text(encoding="utf-8"))
    all_leaf_qs = _flatten_leaf_questions(short_report.get("questions", []))
    page_questions: dict[int, list[dict]] = {}
    for q in all_leaf_qs:
        pg = int(q.get("page") or 0)
        page_questions.setdefault(pg, []).append(q)

    api_call_timings: list[dict] = []

    doc = fitz.open(str(ctx.cleaned_pdf))
    try:
        for i in range(ctx.num_students):
            student_name: str | None = None
            for p in range(1, ctx.pages_per_student + 1):
                scan_idx = i * ctx.pages_per_student + (p - 1)
                info_line(f"Student {i + 1}/{ctx.num_students} · page {p}/{ctx.pages_per_student}")

                b64 = _render_page_b64(doc, scan_idx, dpi=dpi)
                blueprint = json.loads(
                    artifact_blueprint_json_path(ctx.artifact_dir, p).read_text(encoding="utf-8")
                )

                if p == 1:
                    t0 = time.perf_counter()
                    student_name = _identify_student(
                        client, model_id, b64, ctx.students or [], i, extra_body
                    )
                    name_dur = round(time.perf_counter() - t0, 2)
                    api_latency_line(name_dur, label=f"name id · {student_name}")
                    api_call_timings.append({
                        "phase": "name_id",
                        "student_idx": i,
                        "student": student_name,
                        "page": p,
                        "duration_s": name_dur,
                    })

                t0 = time.perf_counter()
                filled = _mark_page(
                    client, model_id, b64, blueprint, page_questions.get(p, []), extra_body
                )
                mark_dur = round(time.perf_counter() - t0, 2)
                api_latency_line(mark_dur, label=f"{student_name} p{p}")
                api_call_timings.append({
                    "phase": "marking",
                    "student": student_name,
                    "page": p,
                    "duration_s": mark_dur,
                })

                filled["student_name"] = student_name
                out_path = artifact_marked_json_path(
                    ctx.artifact_dir, student_name or f"Unknown_{i}", p
                )
                out_path.write_text(json.dumps(filled, indent=2, ensure_ascii=False), encoding="utf-8")
    finally:
        doc.close()

    return api_call_timings
