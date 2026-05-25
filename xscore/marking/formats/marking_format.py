"""The :class:`MarkingFormat` class — AI marking output format (YAML).

YAML block scalars (``|``) preserve literal backslashes, ``{``, ``}``, ``#``,
``$``, so LaTeX content in ``student_answer`` and ``explanation`` needs no
format-level escaping.
"""

from __future__ import annotations

import yaml

from xscore.marking.formats._parsers import (
    FormatParseError,
    _yaml_questions_to_list,
    parse_confidence_int,
    parse_problem,
)
from xscore.marking.formats._prompt_builders import _build_yaml_blueprint
from xscore.marking.formats._yaml_io import _MarkingDumper
from xscore.shared.response_parsing import strip_code_fences as _strip_fences


class MarkingFormat:

    # --- Blueprint construction ---

    def build_blueprint(self, page_num: int, layout, questions: list[dict]) -> str:
        return _build_yaml_blueprint(page_num, layout, questions)

    def validate_blueprint(self, text: str) -> None:
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Blueprint YAML is malformed: {exc}") from exc

    # --- Prompt fragments ---

    def prompt_name(self) -> str:
        return "ai_marking"

    def criterion_ref(self) -> str:
        return "the mark scheme"

    def subpage_ref(self) -> str:
        return "`subpage` entries"

    # --- API enforcement ---

    def api_extra_kwargs(self, model: str) -> dict:
        return {}

    def prefer_stream(self) -> bool:
        return True

    # --- Response parsing ---

    def parse_response(self, raw: str) -> list[dict]:
        try:
            data = yaml.safe_load(_strip_fences(raw))
        except yaml.YAMLError as exc:
            raise FormatParseError(f"YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise FormatParseError(f"YAML: expected a mapping, got {type(data).__name__}")
        questions = data.get("questions", [])
        if not isinstance(questions, list):
            raise FormatParseError("YAML: 'questions' key is not a list")
        return [self._normalize_question_dict(q) for q in questions if isinstance(q, dict)]

    def _normalize_question_dict(self, q: dict) -> dict:
        """One raw AI question dict → canonical parse_response output entry.

        Shared with :meth:`parse_list_fallback` so the two stay in lockstep on
        field aliases (transcribed_answer/student_answer) and integer parsing.
        """
        am = q.get("assigned_marks", "")
        # Mirror _yaml_questions_to_list: empty / null / unparseable becomes
        # None ("AI did not produce a mark"), distinct from a legitimate 0.
        # _apply_marking_response treats None as "still pending" so the
        # completeness retry can re-ask, instead of recording a silent 0.
        try:
            am_int: int | None = int(am) if str(am).strip() not in ("", "null", "None") else None
        except (ValueError, TypeError):
            am_int = None
        # The marking prompt presents the field as `transcribed_answer` to
        # signal it's read-only input. The AI may copy that key through, or
        # fall back to the legacy `student_answer` name from training data.
        # Accept either; downstream uses `student_answer` everywhere.
        _sa = q.get("student_answer")
        if _sa is None:
            _sa = q.get("transcribed_answer")
        return {
            "number":        str(q.get("number", "")),
            "subpage_row":   int(q.get("subpage_row", 1)),
            "subpage_col":   int(q.get("subpage_col", 1)),
            "assigned_marks": am_int,
            "student_answer": str(_sa or "").strip(),
            "corrected_student_answer": str(q.get("corrected_student_answer") or "").strip(),
            "explanation":    str(q.get("explanation") or "").strip(),
            "confidence":     parse_confidence_int(q.get("confidence")),
            "problem":        parse_problem(q.get("problem")),
        }

    def parse_list_fallback(self, raw: str) -> list[dict] | None:
        """Recover a question list from a response where the AI dropped the
        ``questions:`` wrapper and emitted the list at the document root.

        Returns a list of canonical entries (each shaped like one
        :meth:`parse_response` output entry) or None when *raw* doesn't look
        like a list-at-root response. Each entry's ``number`` field is what
        makes this safe without 1×1 gating — entries self-identify, no
        positional ambiguity.
        """
        try:
            data = yaml.safe_load(_strip_fences(raw))
        except yaml.YAMLError:
            return None
        if not isinstance(data, list) or not data:
            return None
        if not all(isinstance(q, dict) and q.get("number") is not None for q in data):
            return None
        return [self._normalize_question_dict(q) for q in data]

    def parse_flat_fallback(self, raw: str) -> dict | None:
        """Recover the four fill fields from a response where the AI dropped
        the ``questions:`` wrapper and emitted them at the document root.

        Returns the four-field dict (matching one entry of ``parse_response``'s
        list, minus ``number``/``subpage_row``/``subpage_col`` which the caller
        attributes from the blueprint) or None when *raw* doesn't look like a
        flat-keyed response. Caller is responsible for gating this on a
        single-question blueprint, since flat-keyed shape gives no way to
        disambiguate which question the fields belong to.
        """
        try:
            data = yaml.safe_load(_strip_fences(raw))
        except yaml.YAMLError:
            return None
        if not isinstance(data, dict):
            return None
        target_keys = {"assigned_marks", "explanation", "confidence", "problem"}
        if not (target_keys & data.keys()):
            return None
        if data.get("questions"):
            return None
        am = data.get("assigned_marks", "")
        try:
            am_int: int | None = int(am) if str(am).strip() not in ("", "null", "None") else None
        except (ValueError, TypeError):
            am_int = None
        # student_answer="" — blueprint pre-fill from extract_student_answers wins via merge.
        return {
            "assigned_marks": am_int,
            "student_answer": "",
            "corrected_student_answer": str(data.get("corrected_student_answer") or "").strip(),
            "explanation":    str(data.get("explanation") or "").strip(),
            "confidence":     parse_confidence_int(data.get("confidence")),
            "problem":        parse_problem(data.get("problem")),
        }

    # --- Serialisation ---

    def serialize_filled(self, filled: dict) -> str:
        _layout = filled.get("layout") or {"rows": 1, "cols": 1}
        _is_grid = int(_layout.get("rows", 1)) > 1 or int(_layout.get("cols", 1)) > 1
        doc: dict = {
            "page": filled.get("page", ""),
            "student_name": filled.get("student_name", ""),
        }
        if _is_grid:
            doc["layout"] = _layout
        doc["questions"] = []
        for q in filled.get("questions") or []:
            am = q.get("assigned_marks")
            cf = q.get("confidence")
            entry: dict = {
                "number":    str(q.get("number", "")),
                "type":      str(q.get("question_type", "")),
                "max_marks": int(q.get("max_marks", 0)),
            }
            if _is_grid:
                entry["subpage_row"] = int(q.get("subpage_row", 1))
                entry["subpage_col"] = int(q.get("subpage_col", 1))
                entry["order_in_subpage"] = int(q.get("order_in_subpage", 1))
            entry["question_text"] = str(q.get("question_text") or "")

            _options = [
                {"letter": str(o.get("letter", "")), "text": str(o.get("text", ""))}
                for o in (q.get("answer_options") or [])
            ]
            if _options:
                entry["options"] = _options

            _criteria = [
                {"mark": str(c.get("mark", "")), "criterion": str(c.get("criterion", ""))}
                for c in (q.get("mark_scheme") or [])
            ]
            if _criteria:
                entry["criteria"] = _criteria

            _ca = str(q.get("correct_answer") or "")
            if _ca:
                entry["correct_answer"] = _ca

            entry["student_answer"] = str(q.get("student_answer") or "")
            entry["assigned_marks"] = int(am) if am is not None else 0
            entry["explanation"] = str(q.get("explanation") or "")
            entry["confidence"] = parse_confidence_int(cf)
            entry["problem"] = str(q.get("problem") or "")
            doc["questions"].append(entry)
        return yaml.dump(
            doc, Dumper=_MarkingDumper,
            allow_unicode=True, default_flow_style=False,
            sort_keys=False,
        )

    def deserialize_blueprint(self, text: str) -> dict:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise FormatParseError(f"YAML blueprint parse error: {exc}") from exc
        if not isinstance(data, dict):
            return {"page": 1, "layout": {"rows": 1, "cols": 1}, "questions": []}
        questions = _yaml_questions_to_list(data)
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
        return "yaml"
