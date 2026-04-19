"""Step 11 — AI marking blueprints: one YAML per exam page, leaf questions only."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def build_blueprints(scaffold: Any, artifact_dir: Path) -> list[dict]:
    """For each exam page create a blueprint YAML with only the leaf questions.

    Each question entry has: number, question_type, max_marks, student_answer (empty),
    assigned_marks (null), reasoning (empty).  Only leaf questions (gradable_questions)
    whose .page field matches the page number are included.

    Returns a list of blueprint dicts, one per exam page (1-indexed).
    """
    import yaml
    from xscore.shared.exam_paths import artifact_blueprint_yaml_path, artifact_blueprint_md_path
    layout = scaffold.layout
    blueprints: list[dict] = []
    for page_num in range(1, scaffold.page_count + 1):
        page_qs = [
            {
                "number": re.sub(r"_\d+$", "", q.number),
                "question_type": q.question_type,
                "subpage_row": q.subpage_row,
                "subpage_col": q.subpage_col,
                "question_text": q.text or "",
                "answer_options": [
                    {"letter": o.letter, "text": o.text}
                    for o in (q.answer_options or [])
                ],
                "max_marks": q.marks,
                "student_answer": "",
                "assigned_marks": None,
                "reasoning": "",
            }
            for q in scaffold.gradable_questions
            if q.page == page_num
        ]
        bp = {
            "page": page_num,
            "layout": {"rows": layout.rows, "cols": layout.cols},
            "questions": page_qs,
        }
        blueprints.append(bp)

        yaml_path = artifact_blueprint_yaml_path(artifact_dir, page_num)
        yaml_path.parent.mkdir(parents=True, exist_ok=True)
        yaml_path.write_text(yaml.dump(bp, allow_unicode=True, sort_keys=False), encoding="utf-8")

        md_path = artifact_blueprint_md_path(artifact_dir, page_num)
        md_path.write_text(_blueprint_to_md(bp), encoding="utf-8")

    return blueprints


def marked_to_md(filled: dict) -> str:
    """Render a completed marking dict as a human-readable markdown table."""
    student = filled.get("student_name", "Unknown")
    page = filled.get("page", "?")
    questions = filled.get("questions", [])
    total_awarded = sum(q.get("assigned_marks") or 0 for q in questions)
    total_max = sum(q.get("max_marks") or 0 for q in questions)
    lines = [
        f"# Marked: {student} — Page {page}",
        "",
        f"**Score: {total_awarded} / {total_max}**",
        "",
        "| # | Type | Max | Answer | Marks | Reasoning |",
        "|---|------|-----|--------|-------|-----------|",
    ]
    for q in questions:
        num = q.get("number", "")
        qtype = (q.get("question_type") or "").replace("_", " ")
        max_m = q.get("max_marks", "")
        ans = (q.get("student_answer") or "—").replace("|", "\\|")
        awarded = q.get("assigned_marks")
        awarded_str = "—" if awarded is None else str(awarded)
        reasoning = (q.get("reasoning") or "").replace("|", "\\|")
        lines.append(f"| {num} | {qtype} | {max_m} | {ans} | {awarded_str} | {reasoning} |")
    return "\n".join(lines) + "\n"


def _blueprint_to_md(bp: dict) -> str:
    lines = [f"# AI Marking Blueprint — Page {bp['page']}\n"]
    if not bp["questions"]:
        lines.append("_No questions assigned to this page._\n")
        return "\n".join(lines)
    lines.append("| # | Type | Max marks |")
    lines.append("|---|------|-----------|")
    for q in bp["questions"]:
        lines.append(f"| {q['number']} | {q['question_type']} | {q['max_marks']} |")
    return "\n".join(lines) + "\n"
