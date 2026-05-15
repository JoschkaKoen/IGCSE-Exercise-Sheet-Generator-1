"""YAML serialisation for the scaffold cache (primary format).

Block-scalar style preserves LaTeX backslashes verbatim. See
:mod:`scaffold_cache_xml` for the legacy XML fallback retained for resume
compatibility with old caches.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from xscore.scaffold.formats.base import _ScaffoldDumper
from xscore.scaffold.scaffold_cache import (
    SCHEMA_VERSION,
    question_from_dict,
    question_to_dict,
    _compute_pdf_sha256,
)
from xscore.shared.models import (
    BBox,
    ExamLayout,
    ExamScaffold,
    McAnswerOption,
    Question,
    gradable_questions,
)


def _question_to_yaml_dict(q: Question) -> dict:
    """Mirror the XML schema in dict form, suitable for ``yaml.safe_dump``.

    Keeps the same field set as ``_question_to_xml_element`` for round-trip
    equivalence between formats. Sparse: omits empty/null fields.
    """
    d: dict[str, Any] = {
        "number": q.number,
        "type": q.question_type,
        "page": q.page or (q.bbox.page if q.bbox else 1),
        "subpage_row": q.subpage_row,
        "subpage_col": q.subpage_col,
        "marks": q.marks,
    }
    if q.correct_answer is not None and str(q.correct_answer).strip():
        d["correct_answer"] = str(q.correct_answer)
    d["text"] = q.text or ""
    if q.answer_options:
        d["options"] = [{"letter": o.letter, "text": o.text} for o in q.answer_options]
    if q.question_type != "multiple_choice" and q.mark_scheme_answer and str(q.mark_scheme_answer).strip():
        d["mark_scheme_answer"] = str(q.mark_scheme_answer)
    if q.explanation and str(q.explanation).strip():
        d["explanation"] = str(q.explanation)
    if q.question_type != "multiple_choice" and q.marking_criteria and str(q.marking_criteria).strip():
        d["marking_criteria"] = str(q.marking_criteria)
    if q.reasoning and str(q.reasoning).strip():
        d["reasoning"] = str(q.reasoning)
    if q.subquestions:
        d["subquestions"] = [_question_to_yaml_dict(s) for s in q.subquestions]
    return d


def _question_from_yaml_dict(d: dict) -> Question:
    page = int(d.get("page") or 1)
    answer_options = [
        McAnswerOption(letter=str(o.get("letter", "")), text=str(o.get("text") or ""))
        for o in (d.get("options") or [])
        if isinstance(o, dict) and o.get("letter")
    ]
    msa_raw = d.get("mark_scheme_answer")
    mark_scheme_answer: str | None = (
        str(msa_raw).strip() or None if msa_raw is not None else None
    )
    marking_criteria_raw = d.get("marking_criteria")
    marking_criteria: str | None = (
        str(marking_criteria_raw).strip() or None if marking_criteria_raw is not None else None
    )
    explanation_raw = d.get("explanation")
    explanation: str | None = (
        str(explanation_raw).strip() or None if explanation_raw is not None else None
    )
    reasoning_raw = d.get("reasoning")
    reasoning: str | None = (
        str(reasoning_raw).strip() or None if reasoning_raw is not None else None
    )
    subquestions = [_question_from_yaml_dict(s) for s in (d.get("subquestions") or []) if isinstance(s, dict)]
    return Question(
        number=str(d.get("number", "")),
        question_type=d.get("type", "short_answer"),
        text=str(d.get("text") or ""),
        marks=int(d.get("marks", 0)),
        bbox=BBox(0.0, 0.0, 0.0, 0.0, page),
        page=page,
        subpage_row=int(d.get("subpage_row", 1)),
        subpage_col=int(d.get("subpage_col", 1)),
        answer_options=answer_options,
        subquestions=subquestions,
        correct_answer=(d.get("correct_answer") or None),
        mark_scheme_answer=mark_scheme_answer,
        explanation=explanation,
        marking_criteria=marking_criteria,
        reasoning=reasoning,
    )


def _scaffold_to_yaml(
    scaffold: ExamScaffold,
    students: list[str] | None = None,
    source_hashes: dict[str, str] | None = None,
) -> str:
    """Serialise ExamScaffold to a YAML string.

    Filenames in *source_hashes* live as YAML string values under
    ``sources[].file`` — never as keys — so they tolerate spaces, leading
    digits, dots, and any other characters that are illegal in XML attribute
    names. This is the structural fix for the malformed-attribute bug in the
    legacy XML cache writer.
    """
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "total_marks": scaffold.total_marks,
        "page_count": scaffold.page_count,
        "layout": {"rows": scaffold.layout.rows, "cols": scaffold.layout.cols},
    }
    if source_hashes:
        payload["sources"] = [
            {"file": name, "sha256": h}
            for name, h in source_hashes.items()
            if h
        ]
    if students:
        payload["students"] = list(students)
    payload["questions"] = [_question_to_yaml_dict(q) for q in scaffold.questions]
    return yaml.safe_dump(
        payload,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=10**9,
    )


def _load_cache_yaml(path: Path) -> ExamScaffold:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"scaffold YAML cache malformed: {path}")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"scaffold YAML schema_version mismatch — rebuild required "
            f"(got {data.get('schema_version')!r}, need {SCHEMA_VERSION!r})"
        )
    questions = [
        _question_from_yaml_dict(q)
        for q in (data.get("questions") or [])
        if isinstance(q, dict)
    ]
    total = int(data.get("total_marks", 0))
    if not total and questions:
        total = sum(q.marks for q in gradable_questions(questions))
    layout_d = data.get("layout") or {}
    return ExamScaffold(
        questions=questions,
        total_marks=total,
        page_count=int(data.get("page_count", 0)),
        layout=ExamLayout(
            rows=int(layout_d.get("rows", 1)),
            cols=int(layout_d.get("cols", 1)),
        ),
    )


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

