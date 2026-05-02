"""Step 12 — AI marking blueprints: one XML per exam page, leaf questions only."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any


def _clean_text(text: str) -> str:
    """Decode HTML entities left as literals after scaffold XML round-trip,
    then collapse long fill-in-the-blank ellipsis runs to a short placeholder."""
    text = html.unescape(text)
    text = re.sub(r'\u2026{2,}', '…', text)
    text = re.sub(r'\.{6,}', '…', text)
    return text


def _quadrant_label(row: int, col: int, total_rows: int, total_cols: int) -> str:
    v = "top" if row == 1 else "bottom" if row == total_rows else f"row {row}"
    h = "left" if col == 1 else "right" if col == total_cols else f"col {col}"
    return f"{v}-{h}"


def _criteria_from_marking_str(criteria_str: str | None) -> list[tuple[str, str]]:
    """Return the full marking criteria block as a single (mark, text) entry."""
    if not criteria_str or not criteria_str.strip():
        return []
    return [("", criteria_str.strip())]


def _build_blueprint_xml(page_num: int, layout: Any, page_qs: list[dict]) -> str:
    """Build <marking page rows cols> XML for one exam page."""
    root = ET.Element("marking")
    root.set("page", str(page_num))
    root.set("rows", str(layout.rows))
    root.set("cols", str(layout.cols))
    if layout.rows > 1 or layout.cols > 1:
        for r in range(1, layout.rows + 1):
            for c in range(1, layout.cols + 1):
                sp = ET.SubElement(root, "subpage")
                sp.set("row", str(r))
                sp.set("col", str(c))
                sp.set("label", _quadrant_label(r, c, layout.rows, layout.cols))
    for q in page_qs:
        qel = ET.SubElement(root, "question")
        qel.set("number", str(q["number"]))
        qel.set("type", str(q["question_type"]))
        qel.set("subpage_row", str(q["subpage_row"]))
        qel.set("subpage_col", str(q["subpage_col"]))
        qel.set("order_in_subpage", str(q["order_in_subpage"]))
        qel.set("max_marks", str(q["max_marks"]))
        if q.get("correct_answer") is not None:
            qel.set("correct_answer", str(q["correct_answer"]))
        text_el = ET.SubElement(qel, "text")
        text_el.text = _clean_text(q.get("question_text", ""))
        for opt in (q.get("answer_options") or []):
            opt_el = ET.SubElement(qel, "option")
            opt_el.set("letter", str(opt.get("letter", "")))
            opt_el.text = _clean_text(opt.get("text", ""))
        if q.get("question_type") != "multiple_choice":
            for mark, ctext in _criteria_from_marking_str(q.get("marking_criteria")):
                cel = ET.SubElement(qel, "criterion")
                cel.set("mark", mark)
                cel.text = _clean_text(ctext)
        ET.SubElement(qel, "student_answer")
        ET.SubElement(qel, "assigned_marks")
        ET.SubElement(qel, "explanation")
        # Side-channel signals — do NOT affect marking or PDFs. Read only
        # by step 34 (review queue / confidence audit).
        # confidence: int 0–10 (0 = no confidence, 10 = fully certain).
        # problem: short freeform string the AI fills in when there's a
        # specific concern worth a human glance; empty otherwise.
        ET.SubElement(qel, "confidence")
        ET.SubElement(qel, "problem")
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=False, short_empty_elements=False)


def build_blueprints(scaffold: Any, artifact_dir: Path) -> list[dict]:
    """For each exam page create a blueprint with only the leaf questions.

    Returns a list of blueprint dicts, one per exam page (1-indexed).
    """
    from xscore.marking.formats import get_marking_format
    from xscore.shared.exam_paths import artifact_blueprint_md_path, artifact_blueprint_path
    fmt = get_marking_format()
    layout = scaffold.layout
    blueprints: list[dict] = []
    for page_num in range(1, scaffold.page_count + 1):
        subpage_counters: dict[tuple[int, int], int] = {}
        page_qs = []
        for q in scaffold.gradable_questions:
            if q.page != page_num:
                continue
            key = (q.subpage_row, q.subpage_col)
            subpage_counters[key] = subpage_counters.get(key, 0) + 1
            page_qs.append({
                "number": re.sub(r"_\d+$", "", q.number),
                "question_type": q.question_type,
                "subpage_row": q.subpage_row,
                "subpage_col": q.subpage_col,
                "order_in_subpage": subpage_counters[key],
                "question_text": q.text or "",
                "answer_options": [
                    {"letter": o.letter, "text": o.text}
                    for o in (q.answer_options or [])
                ],
                "correct_answer": q.correct_answer,
                "marking_criteria": q.marking_criteria,
                "max_marks": q.marks,
                "student_answer": "",
                "assigned_marks": None,
                "explanation": "",
            })
        bp = {
            "page": page_num,
            "layout": {"rows": layout.rows, "cols": layout.cols},
            "questions": page_qs,
        }
        blueprints.append(bp)

        bp_path = artifact_blueprint_path(artifact_dir, page_num, fmt=fmt.artifact_ext())
        bp_path.parent.mkdir(parents=True, exist_ok=True)
        _bp_text = fmt.build_blueprint(page_num, layout, page_qs)
        fmt.validate_blueprint(_bp_text)
        bp_path.write_text(_bp_text, encoding="utf-8")

        md_path = artifact_blueprint_md_path(artifact_dir, page_num)
        md_path.write_text(_blueprint_to_md(bp), encoding="utf-8")

    return blueprints


def marked_to_md(filled: dict) -> str:
    """Render a completed ``14_marked_*.json`` as a human-readable markdown table."""
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
        reasoning = (q.get("explanation") or "").replace("|", "\\|")
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
