"""JSON marking format — native schema enforcement (Pydantic / strict json_schema).

Gemini models receive ``response_schema=MarkingResponse``.
OpenAI-compat models (Qwen, Grok, …) receive
``response_format={"type": "json_schema", "json_schema": {"strict": True, ...}}``;
falls back to ``{"type": "json_object"}`` if strict is rejected.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from xscore.marking.formats.base import FormatParseError, MarkingFormat
from xscore.shared.response_parsing import strip_code_fences as _strip_fences  # noqa: F401


# ---------------------------------------------------------------------------
# Pydantic schema
# ---------------------------------------------------------------------------

class _QuestionMarking(BaseModel):
    number: str
    subpage_row: int = 1
    subpage_col: int = 1
    student_answer: str
    assigned_marks: int
    explanation: str
    # Side-channel confidence — does NOT influence marks or PDF.
    # Required so strict json_schema validation accepts the field; an empty
    # string is acceptable and downstream code treats empty as "high".
    confidence: str = ""


class _MarkingResponse(BaseModel):
    questions: list[_QuestionMarking]


_MARKING_JSON_SCHEMA = _MarkingResponse.model_json_schema()




class JsonMarkingFormat(MarkingFormat):

    # --- Blueprint construction ---

    def build_blueprint(self, page_num: int, layout, questions: list[dict]) -> str:
        """Build JSON blueprint — same structure as YAML but serialised as JSON."""
        import yaml  # reuse YAML blueprint builder then re-encode as JSON
        from xscore.marking.formats.yaml_format import _build_yaml_blueprint, _MarkingDumper
        yaml_str = _build_yaml_blueprint(page_num, layout, questions)
        data = yaml.safe_load(yaml_str)
        return json.dumps(data, ensure_ascii=False, indent=2)

    def validate_blueprint(self, text: str) -> None:
        try:
            json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Blueprint JSON is malformed: {exc}") from exc

    # --- Prompt fragments ---

    def prompt_name(self) -> str:
        return "ai_marking_json"

    def criterion_ref(self) -> str:
        return "`criteria` entries"

    def subpage_ref(self) -> str:
        return "`subpage` entries"

    # --- API enforcement ---

    def api_extra_kwargs(self, model: str) -> dict:
        if model.startswith("gemini"):
            return {
                "response_mime_type": "application/json",
                "response_schema": _MarkingResponse,
            }
        # OpenAI-compat (Qwen, Grok, …). Marking uses a system message, so on
        # providers that reject json_schema together with a system message
        # (Qwen/DashScope) we fall back to json_object.
        from eXercise.ai_client import (
            provider_for_model,
            provider_supports_json_schema_with_system,
        )
        if provider_supports_json_schema_with_system(provider_for_model(model)):
            return {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "marking_response",
                        "strict": True,
                        "schema": _MARKING_JSON_SCHEMA,
                    },
                }
            }
        return {"response_format": {"type": "json_object"}}

    def prefer_stream(self) -> bool:
        return False

    # --- Response parsing ---

    def parse_response(self, raw: str) -> list[dict]:
        try:
            data = json.loads(_strip_fences(raw))
        except json.JSONDecodeError as exc:
            raise FormatParseError(f"JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise FormatParseError(f"JSON: expected an object, got {type(data).__name__}")
        questions = data.get("questions", [])
        if not isinstance(questions, list):
            raise FormatParseError("JSON: 'questions' key is not an array")
        result = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            try:
                am = int(q.get("assigned_marks", 0))
            except (ValueError, TypeError):
                am = 0
            result.append({
                "number":        str(q.get("number", "")),
                "subpage_row":   int(q.get("subpage_row", 1)),
                "subpage_col":   int(q.get("subpage_col", 1)),
                "assigned_marks": am,
                "student_answer": str(q.get("student_answer") or "").strip(),
                "explanation":    str(q.get("explanation") or "").strip(),
                "confidence":     str(q.get("confidence") or "").strip().lower(),
            })
        return result

    # --- Serialisation ---

    def serialize_filled(self, filled: dict) -> str:
        doc: dict = {
            "page": filled.get("page", ""),
            "student_name": filled.get("student_name", ""),
            "layout": filled.get("layout") or {"rows": 1, "cols": 1},
            "questions": [],
        }
        for q in filled.get("questions") or []:
            am = q.get("assigned_marks")
            doc["questions"].append({
                "number":           str(q.get("number", "")),
                "type":             str(q.get("question_type", "")),
                "subpage_row":      int(q.get("subpage_row", 1)),
                "subpage_col":      int(q.get("subpage_col", 1)),
                "order_in_subpage": int(q.get("order_in_subpage", 1)),
                "max_marks":        int(q.get("max_marks", 0)),
                "correct_answer":   str(q.get("correct_answer") or ""),
                "text":             str(q.get("question_text") or ""),
                "criteria":         [
                    {"mark": str(c.get("mark", "")), "criterion": str(c.get("criterion", ""))}
                    for c in (q.get("mark_scheme") or [])
                ],
                "options":          [
                    {"letter": str(o.get("letter", "")), "text": str(o.get("text", ""))}
                    for o in (q.get("answer_options") or [])
                ],
                "student_answer":   str(q.get("student_answer") or ""),
                "assigned_marks":   int(am) if am is not None else 0,
                "explanation":      str(q.get("explanation") or ""),
                "confidence":       str(q.get("confidence") or ""),
            })
        return json.dumps(doc, ensure_ascii=False, indent=2)

    def deserialize_blueprint(self, text: str) -> dict:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise FormatParseError(f"JSON blueprint parse error: {exc}") from exc
        if not isinstance(data, dict):
            return {"page": 1, "layout": {"rows": 1, "cols": 1}, "questions": []}
        questions = []
        for q in data.get("questions", []):
            if not isinstance(q, dict):
                continue
            am_raw = q.get("assigned_marks", "")
            try:
                am_int: int | None = int(am_raw) if str(am_raw).strip() not in ("", "null", "None") else None
            except (ValueError, TypeError):
                am_int = None
            questions.append({
                "number":           str(q.get("number", "")),
                "question_type":    str(q.get("type", "short_answer")),
                "subpage_row":      int(q.get("subpage_row", 1)),
                "subpage_col":      int(q.get("subpage_col", 1)),
                "order_in_subpage": int(q.get("order_in_subpage", 1)),
                "question_text":    str(q.get("text", "")),
                "answer_options":   [
                    {"letter": str(o.get("letter", "")), "text": str(o.get("text", ""))}
                    for o in (q.get("options") or [])
                ],
                # str() wrap: model occasionally emits unquoted numeric scalar (e.g. `"correct_answer": 5`); parser must coerce.
                "correct_answer":   str(q.get("correct_answer") or "").strip() or None,
                "max_marks":        int(q.get("max_marks", 0)),
                "mark_scheme":      [
                    {"mark": str(c.get("mark", "")), "criterion": str(c.get("criterion", ""))}
                    for c in (q.get("criteria") or [])
                ],
                "student_answer":   str(q.get("student_answer") or "").strip(),
                "assigned_marks":   am_int,
                "explanation":      str(q.get("explanation") or "").strip(),
                "confidence":       str(q.get("confidence") or "").strip().lower(),
            })
        result: dict = {
            "page":     int(data.get("page", 1)),
            "layout":   data.get("layout") or {"rows": 1, "cols": 1},
            "questions": questions,
        }
        student_name = data.get("student_name", "")
        if student_name:
            result["student_name"] = str(student_name)
        return result

    def artifact_ext(self) -> str:
        return "json"
