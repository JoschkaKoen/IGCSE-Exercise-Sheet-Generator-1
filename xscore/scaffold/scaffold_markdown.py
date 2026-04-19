"""Write a human-readable ``scaffold.md`` from the same dict payload as ``scaffold.json``."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from xscore.shared.exam_paths import (
    artifact_exam_questions_markdown_path,
    artifact_mark_scheme_markdown_path,
    artifact_scaffold_markdown_path,
    artifact_short_scaffold_markdown_path,
)


def _heading_prefix(depth: int) -> str:
    level = min(3 + depth, 6)
    return "#" * level


def _type_label(question_type: str) -> str:
    return str(question_type).replace("_", " ").strip() or "question"


def _marks_word(marks: int) -> str:
    n = int(marks)
    return "1 mark" if n == 1 else f"{n} marks"


def _strip_fill_in_dots(text: str) -> str:
    """Remove trailing lines that are only dot runs (Cambridge answer lines)."""
    lines = text.split("\n")
    while lines and re.match(r"^\s*\.+\s*$", lines[-1]):
        lines.pop()
    return "\n".join(lines).rstrip()


def _escape_table_cell(s: str) -> str:
    return s.replace("|", "\\|")


def _format_prose_block(label: str, value: str) -> list[str]:
    out: list[str] = []
    if "\n" in value.strip():
        out.append(f"**{label}:**")
        out.append("")
        for line in value.split("\n"):
            out.append(f"> {line}")
    else:
        out.append(f"**{label}:** {value}")
    return out


def _render_question(q: dict[str, Any], depth: int, lines: list[str]) -> None:
    num = q.get("number", "")
    qtype = _type_label(str(q.get("question_type", "")))
    marks = int(q.get("marks", 0))
    page = q.get("page", "")
    hp = _heading_prefix(depth)
    page_str = f" · p{page}" if page else ""
    lines.append(f"{hp} Q{num} · {qtype} · {_marks_word(marks)}{page_str}")
    lines.append("")

    text = q.get("text")
    if isinstance(text, str) and text.strip():
        lines.append(_strip_fill_in_dots(text))
        lines.append("")

    opts = q.get("answer_options")
    if isinstance(opts, list) and opts:
        lines.append("**Options**")
        lines.append("")
        lines.append("| Letter | Text |")
        lines.append("|--------|------|")
        for o in opts:
            if not isinstance(o, dict):
                continue
            letter = _escape_table_cell(str(o.get("letter", "")))
            ot = _escape_table_cell(str(o.get("text") or ""))
            lines.append(f"| {letter} | {ot} |")
        lines.append("")

    for img_key in ("images", "answer_images"):
        imgs = q.get(img_key)
        if not isinstance(imgs, list) or not imgs:
            continue
        label = "Image" if img_key == "images" else "Answer image"
        for im in imgs:
            if isinstance(im, dict) and im.get("path"):
                lines.append(f"**{label}:** `{im['path']}`")
                lines.append("")

    ca = q.get("correct_answer")
    if isinstance(ca, str) and ca.strip():
        lines.extend(_format_prose_block("Answer", ca.strip()))
        lines.append("")

    mc = q.get("marking_criteria")
    if isinstance(mc, str) and mc.strip():
        lines.extend(_format_prose_block("Marking criteria", mc.strip()))
        lines.append("")

    subs = q.get("subquestions")
    if isinstance(subs, list) and subs:
        for s in subs:
            if isinstance(s, dict):
                _render_question(s, depth + 1, lines)


def _write_scaffold_markdown(path: Path, payload: dict[str, Any]) -> None:
    """Render scaffold markdown from *payload* and write it to *path*."""
    lines: list[str] = []
    lines.append("# Exam Report")
    lines.append("")

    students = payload.get("students")
    if isinstance(students, list) and students:
        lines.append("## Students")
        lines.append("")
        lines.append(f"**{len(students)} students on the roster**")
        lines.append("")
        for i, name in enumerate(students, 1):
            lines.append(f"{i}. {name}")
        lines.append("")

    sv = payload.get("schema_version", "")
    tm = payload.get("total_marks", "")
    pc = payload.get("page_count", "")
    lines.append(f"**Schema version:** {sv} · **Total marks:** {tm} · **Pages:** {pc}")
    lines.append("")

    raw = payload.get("raw_description")
    if isinstance(raw, str) and raw.strip():
        lines.append("## Summary")
        lines.append("")
        lines.append(raw.strip())
        lines.append("")

    questions = payload.get("questions")
    if isinstance(questions, list) and questions:
        lines.append("## Questions")
        lines.append("")
        first = True
        for q in questions:
            if not isinstance(q, dict):
                continue
            if not first:
                lines.append("---")
                lines.append("")
            first = False
            _render_question(q, 0, lines)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_scaffold_markdown(artifact_dir: Path, payload: dict[str, Any]) -> None:
    """Write ``6_report.md`` next to ``6_report.yaml`` (same folder as *artifact_dir*)."""
    _write_scaffold_markdown(artifact_scaffold_markdown_path(artifact_dir), payload)


def write_short_scaffold_markdown(artifact_dir: Path, payload: dict[str, Any]) -> None:
    """Write ``6_short_report.md`` — same as ``6_report.md`` but without the students section."""
    short_payload = {k: v for k, v in payload.items() if k != "students"}
    _write_scaffold_markdown(artifact_short_scaffold_markdown_path(artifact_dir), short_payload)


def write_raw_exam_markdown(artifact_dir: Path, raw_questions: list[Any]) -> None:
    """Write ``4_exam_questions.md`` — exam questions without mark-scheme annotations.

    Reuses :func:`_render_question`; since ``correct_answer`` / ``marking_criteria``
    are absent at this stage the renderer omits those sections automatically.
    """
    marks_sum = sum(int(q.get("marks", 0)) for q in raw_questions if isinstance(q, dict))
    n = len(raw_questions)
    lines: list[str] = [
        "# Exam Questions (raw parse)",
        "",
        f"**Questions:** {n} top-level · **Marks (sum):** {marks_sum}",
        "",
        "## Questions",
        "",
    ]
    first = True
    for q in raw_questions:
        if not isinstance(q, dict):
            continue
        if not first:
            lines.append("---")
            lines.append("")
        first = False
        _render_question(q, 0, lines)

    path = artifact_exam_questions_markdown_path(artifact_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_mark_scheme_markdown(artifact_dir: Path, scheme_questions: list[Any]) -> None:
    """Write ``5_mark_scheme.md`` — per-question sections from the raw Gemini scheme output.

    *scheme_questions* is the ``scheme_data["questions"]`` list returned by Gemini before
    merging into the question tree.  Each entry may contain:
    - ``number``: question identifier
    - ``correct_answer``: short answer string (e.g. "C" for multiple choice)
    - ``mark_scheme``: list of ``{"mark": "M1", "criterion": "..."}`` dicts
    """
    lines: list[str] = ["# Mark Scheme", ""]
    first = True
    for q in scheme_questions:
        if not isinstance(q, dict):
            continue
        num = q.get("number", "")
        marks = q.get("marks")
        marks_str = f" · {_marks_word(int(marks))}" if marks else ""
        if not first:
            lines.append("---")
            lines.append("")
        first = False
        lines.append(f"### Q{num}{marks_str}")
        lines.append("")

        ca = q.get("correct_answer")
        if isinstance(ca, str) and ca.strip():
            lines.extend(_format_prose_block("Answer", ca.strip()))
            lines.append("")

        raw_criteria = q.get("mark_scheme") or []
        if isinstance(raw_criteria, list) and raw_criteria:
            criteria_lines = []
            for m in raw_criteria:
                if not isinstance(m, dict) or not m.get("criterion"):
                    continue
                mark = m.get("mark") or ""
                criterion = m.get("criterion", "")
                criteria_lines.append(f"[{mark}] {criterion}".strip() if mark else criterion)
            if criteria_lines:
                lines.extend(_format_prose_block("Marking criteria", "\n".join(criteria_lines)))
                lines.append("")

    path = artifact_mark_scheme_markdown_path(artifact_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
