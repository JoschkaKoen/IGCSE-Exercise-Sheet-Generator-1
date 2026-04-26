"""JSON scaffold format — Pydantic schema enforcement for exam and scheme extraction."""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel

from xscore.prompts.loader import load_prompt
from xscore.scaffold.formats.base import ScaffoldFormat


# ---------------------------------------------------------------------------
# Pydantic schemas
# ---------------------------------------------------------------------------

class _McOption(BaseModel):
    letter: str
    text: str = ""


class _SubQuestion(BaseModel):
    number: str
    type: Literal["multiple_choice", "short_answer", "calculation", "long_answer"] = "short_answer"
    page: int = 1
    subpage_row: int = 1
    subpage_col: int = 1
    marks: int = 0
    text: str = ""
    options: list[_McOption] = []


class _ExamQuestion(_SubQuestion):
    subquestions: list[_SubQuestion] = []


class _ExamResponse(BaseModel):
    rows: int = 1
    cols: int = 1
    questions: list[_ExamQuestion]


class _SchemeCriterion(BaseModel):
    mark: str = ""
    criterion: str


class _SchemeQuestion(BaseModel):
    number: str
    correct_answer: str = ""
    criteria: list[_SchemeCriterion] = []


class _SchemeResponse(BaseModel):
    questions: list[_SchemeQuestion]


_SCHEME_JSON_SCHEMA = _SchemeResponse.model_json_schema()


from xscore.shared.response_parsing import strip_code_fences as _strip_fences  # noqa: E402


class JsonScaffoldFormat(ScaffoldFormat):

    def system_exam_prompt(self) -> str:
        return load_prompt("parse_exam_pdf_json", section="system")[1]

    def system_scheme_prompt(self, is_cs: bool = False) -> str:
        from xscore.scaffold.scaffold_prompts import make_system_scheme_prompt
        return make_system_scheme_prompt("parse_mark_scheme_json", is_cs=is_cs)

    def build_exam_prompt(self, layout_result, is_split: bool, n_split_pages: int) -> str:
        user_exam = load_prompt("parse_exam_pdf_json", section="user")[1]
        if layout_result is None:
            return user_exam
        rows, cols = layout_result.rows, layout_result.cols
        header = (
            f"Layout: {rows}\u00d7{cols} grid. "
            + (f"PDF pre-split into {n_split_pages} sub-pages.\n\n" if is_split else "\n\n")
        )
        return header + user_exam

    def build_scheme_user_msg(
        self, scaffold_str: str, page_num: int, n_pages: int,
        input_label: str = "PDF",
    ) -> str:
        page_note = (
            f"\n\nNote: the {input_label} contains only page {page_num} of {n_pages} "
            "of the mark scheme. Only fill in `correct_answer` and `criteria` for "
            "questions on this page. Leave `correct_answer` as `\"\"` and `criteria` as `[]` "
            "for all other questions."
        )
        return load_prompt(
            "parse_mark_scheme_json", section="user", scaffold=scaffold_str,
        )[1] + page_note

    def build_scheme_scaffold(self, questions: list[dict]) -> str:
        entries = []

        def _visit(node: dict) -> None:
            entries.append({
                "number": str(node.get("number", "")),
                "type": str(node.get("question_type", "")),
                "marks": int(node.get("marks", 0)),
                "correct_answer": "",
                "criteria": [],
            })
            for sub in (node.get("subquestions") or []):
                _visit(sub)

        for q in questions:
            _visit(q)
        return json.dumps({"questions": entries}, ensure_ascii=False, indent=2)

    def extract_question_numbers(self, scaffold_str: str) -> list[str]:
        try:
            data = json.loads(scaffold_str)
            return [
                str(q.get("number", ""))
                for q in data.get("questions", [])
                if isinstance(q, dict) and q.get("number")
            ]
        except (json.JSONDecodeError, AttributeError):
            return []

    def parse_exam_response(self, raw: str) -> tuple[list[dict], dict]:
        try:
            data = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Exam JSON parse error: {exc}") from exc
        layout = {"rows": int(data.get("rows", 1)), "cols": int(data.get("cols", 1))}
        questions = [_parse_json_question(q) for q in data.get("questions", []) if isinstance(q, dict)]
        return questions, layout

    def parse_scheme_response(self, raw: str) -> dict:
        try:
            data = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Mark scheme JSON parse error: {exc}") from exc
        if not isinstance(data, dict):
            return {"questions": []}
        questions = []
        for q in data.get("questions", []):
            if not isinstance(q, dict):
                continue
            questions.append({
                "number":         str(q.get("number", "")),
                "correct_answer": q.get("correct_answer") or None,
                "mark_scheme": [
                    {"mark": str(c.get("mark", "")), "criterion": str(c.get("criterion", ""))}
                    for c in (q.get("criteria") or [])
                    if isinstance(c, dict)
                ],
                "graphics": [],
            })
        return {"questions": questions}

    def serialize_exam(self, questions: list[dict], layout: dict) -> str:
        doc = {
            "rows": layout.get("rows", 1),
            "cols": layout.get("cols", 1),
            "questions": [_exam_q_to_json_dict(q) for q in questions],
        }
        return json.dumps(doc, ensure_ascii=False, indent=2)

    def pydantic_schema_exam(self):
        return _ExamResponse

    def pydantic_schema_scheme(self):
        return _SchemeResponse

    def scheme_oa_extra_kwargs(self, model: str) -> dict:
        # Mark-scheme parsing uses a system message, so on providers that
        # reject json_schema together with a system message (Qwen/DashScope)
        # we fall back to json_object.
        from eXercise.ai_client import (
            provider_for_model,
            provider_supports_json_schema_with_system,
        )
        if provider_supports_json_schema_with_system(provider_for_model(model)):
            return {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "scheme_response",
                        "strict": True,
                        "schema": _SCHEME_JSON_SCHEMA,
                    },
                }
            }
        return {"response_format": {"type": "json_object"}}

    def artifact_ext(self) -> str:
        return "json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_question(q: dict) -> dict:
    return {
        "number":        str(q.get("number", "")),
        "question_type": str(q.get("type", "short_answer")),
        "page":          int(q.get("page", 1)),
        "subpage_row":   int(q.get("subpage_row", 1)),
        "subpage_col":   int(q.get("subpage_col", 1)),
        "marks":         int(q.get("marks", 0)),
        "text":          str(q.get("text", "")),
        "answer_options": [
            {"letter": str(o.get("letter", "")), "text": str(o.get("text", ""))}
            for o in (q.get("options") or [])
            if isinstance(o, dict)
        ],
        "subquestions": [
            _parse_json_question(s) for s in (q.get("subquestions") or [])
            if isinstance(s, dict)
        ],
    }


def _exam_q_to_json_dict(q: dict) -> dict:
    entry: dict = {
        "number":      str(q.get("number", "")),
        "type":        str(q.get("question_type", "short_answer")),
        "page":        int(q.get("page", 1)),
        "subpage_row": int(q.get("subpage_row", 1)),
        "subpage_col": int(q.get("subpage_col", 1)),
        "marks":       int(q.get("marks", 0)),
        "text":        str(q.get("text", "")),
        "options": [
            {"letter": str(o.get("letter", "")), "text": str(o.get("text", ""))}
            for o in (q.get("answer_options") or [])
        ],
        "subquestions": [_exam_q_to_json_dict(s) for s in (q.get("subquestions") or [])],
    }
    return entry
