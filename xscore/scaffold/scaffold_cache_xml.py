"""XML serialisation for the scaffold cache.

Legacy code path retained for resume compatibility with pre-YAML caches.
The primary format is now YAML (see :mod:`scaffold_cache_yaml`); this
module exists so :func:`_load_cache` can fall back when an old XML cache
file is found on disk.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import yaml as _yaml  # only for parsing graphics blobs

from xscore.scaffold.formats.base import _mcq_default_points
from xscore.scaffold.scaffold_cache import (
    SCHEMA_VERSION,
    _bbox_from_dict,
    _bbox_to_dict,
    _img_from_dict,
    _img_to_dict,
    _wa_from_dict,
    _wa_to_dict,
    _compute_pdf_sha256,
)
from xscore.shared.models import (
    BBox,
    ExamImage,
    ExamLayout,
    ExamScaffold,
    McAnswerOption,
    Question,
    WritingArea,
    gradable_questions,
)


def _criterion_str_to_elements(criteria_str: str) -> list[ET.Element]:
    """Convert a LaTeX-formatted marking criteria block → single <criterion mark=""> element."""
    text = criteria_str.strip()
    if not text:
        return []
    el = ET.Element("criterion")
    el.set("mark", "")
    el.text = text
    return [el]


def _question_to_xml_element(q: Question) -> ET.Element:
    el = ET.Element("question")
    el.set("number", q.number)
    el.set("type", q.question_type)
    el.set("page", str(q.page or (q.bbox.page if q.bbox else 1)))
    el.set("subpage_row", str(q.subpage_row))
    el.set("subpage_col", str(q.subpage_col))
    el.set("marks", str(q.marks))
    if q.correct_answer is not None and str(q.correct_answer).strip():
        el.set("correct_answer", str(q.correct_answer))
    text_el = ET.SubElement(el, "text")
    text_el.text = q.text or ""
    for opt in (q.answer_options or []):
        opt_el = ET.SubElement(el, "option")
        opt_el.set("letter", opt.letter)
        opt_el.text = opt.text
    if q.question_type != "multiple_choice" and q.mark_scheme_answer and str(q.mark_scheme_answer).strip():
        msa_el = ET.SubElement(el, "mark_scheme_answer")
        msa_el.text = str(q.mark_scheme_answer)
    elif q.question_type != "multiple_choice" and q.marking_criteria and str(q.marking_criteria).strip():
        # Legacy fallback for in-flight transitions where annotator hasn't yet set mark_scheme_answer.
        for crit_el in _criterion_str_to_elements(str(q.marking_criteria)):
            el.append(crit_el)
    if q.explanation and str(q.explanation).strip():
        exp_el = ET.SubElement(el, "explanation")
        exp_el.text = str(q.explanation)
    elif q.reasoning and str(q.reasoning).strip():
        reasoning_el = ET.SubElement(el, "reasoning")
        reasoning_el.text = str(q.reasoning)
    for sub in (q.subquestions or []):
        el.append(_question_to_xml_element(sub))
    return el



def _scaffold_to_xml(
    scaffold: ExamScaffold,
    students: list[str] | None = None,
    source_hashes: dict[str, str] | None = None,
) -> str:
    """Serialise ExamScaffold to an XML string.

    *source_hashes* maps source-PDF basename → SHA-256 hex digest; each entry
    is written as a ``<basename>_sha256`` attribute on the root so the next
    cache-validity check can compare current file content against the
    snapshot taken at save time. mtime is too fragile (cp -p / touch).
    """
    root = ET.Element("scaffold")
    root.set("schema_version", str(SCHEMA_VERSION))
    root.set("total_marks", str(scaffold.total_marks))
    root.set("page_count", str(scaffold.page_count))
    root.set("rows", str(scaffold.layout.rows))
    root.set("cols", str(scaffold.layout.cols))
    for name, h in (source_hashes or {}).items():
        if h:
            root.set(f"{name}_sha256", h)
    if students:
        studs_el = ET.SubElement(root, "students")
        for s in students:
            s_el = ET.SubElement(studs_el, "student")
            s_el.text = s
    for q in scaffold.questions:
        root.append(_question_to_xml_element(q))
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", xml_declaration=False)


def _question_from_xml_element(el: ET.Element) -> Question:
    page = int(el.get("page", 1))
    text_el = el.find("text")
    text = (text_el.text or "").strip() if text_el is not None else ""
    answer_options = [
        McAnswerOption(letter=o.get("letter", ""), text=(o.text or "").strip())
        for o in el.findall("option")
    ]
    msa_el = el.find("mark_scheme_answer")
    mark_scheme_answer: str | None = (
        (msa_el.text or "").strip() or None
        if msa_el is not None else None
    )
    criterion_parts = []
    for c in el.findall("criterion"):
        mark = c.get("mark", "")
        ctext = (c.text or "").strip()
        if ctext:
            criterion_parts.append(f"[{mark}] {ctext}" if mark else ctext)
    marking_criteria: str | None = "\n".join(criterion_parts) or None
    exp_el = el.find("explanation")
    explanation: str | None = (
        (exp_el.text or "").strip() or None
        if exp_el is not None else None
    )
    reasoning_el = el.find("reasoning")
    reasoning: str | None = (
        (reasoning_el.text or "").strip() or None
        if reasoning_el is not None else None
    )
    subquestions = [_question_from_xml_element(sub) for sub in el.findall("question")]
    return Question(
        number=el.get("number", ""),
        question_type=el.get("type", "short_answer"),
        text=text,
        marks=int(el.get("marks", 0)),
        bbox=BBox(0.0, 0.0, 0.0, 0.0, page),
        page=page,
        subpage_row=int(el.get("subpage_row", 1)),
        subpage_col=int(el.get("subpage_col", 1)),
        answer_options=answer_options,
        subquestions=subquestions,
        correct_answer=el.get("correct_answer") or None,
        mark_scheme_answer=mark_scheme_answer,
        explanation=explanation,
        marking_criteria=marking_criteria,
        reasoning=reasoning,
    )


def _load_cache_xml(path: Path) -> ExamScaffold:
    tree = ET.parse(path)
    root = tree.getroot()
    if root.get("schema_version") != str(SCHEMA_VERSION):
        raise ValueError(
            f"scaffold XML schema_version mismatch — rebuild required "
            f"(got {root.get('schema_version')!r}, need {str(SCHEMA_VERSION)!r})"
        )
    questions = [_question_from_xml_element(el) for el in root.findall("question")]
    total = int(root.get("total_marks", 0))
    if not total and questions:
        total = sum(q.marks for q in gradable_questions(questions))
    return ExamScaffold(
        questions=questions,
        total_marks=total,
        page_count=int(root.get("page_count", 0)),
        layout=ExamLayout(
            rows=int(root.get("rows", 1)),
            cols=int(root.get("cols", 1)),
        ),
    )


# ---------------------------------------------------------------------------
# YAML (de)serialization — primary format
# ---------------------------------------------------------------------------

