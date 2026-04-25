"""YAML marking format — block scalars for zero LaTeX escaping.

block scalars (``|``) preserve literal backslashes, ``{``, ``}``, ``#``, ``$``,
so LaTeX content in student_answer and explanation needs no format-level escaping.
"""

from __future__ import annotations

import yaml

from xscore.marking.formats.base import FormatParseError, MarkingFormat
from xscore.shared.response_parsing import strip_code_fences as _strip_fences


# ---------------------------------------------------------------------------
# Custom YAML dumper — uses literal block scalars for strings with backslashes
# or newlines; plain scalars otherwise.
# ---------------------------------------------------------------------------

class _MarkingDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    if "\n" in data or "\\" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_MarkingDumper.add_representer(str, _str_representer)


def _build_yaml_blueprint(page_num: int, layout, questions: list[dict]) -> str:
    """Build a YAML blueprint string for one exam page."""
    doc: dict = {
        "page": page_num,
        "layout": {"rows": layout.rows, "cols": layout.cols},
        "questions": [],
    }
    if layout.rows > 1 or layout.cols > 1:
        subpages = []
        for r in range(1, layout.rows + 1):
            for c in range(1, layout.cols + 1):
                from xscore.marking.blueprints import _quadrant_label
                subpages.append({
                    "row": r, "col": c,
                    "label": _quadrant_label(r, c, layout.rows, layout.cols),
                })
        doc["subpages"] = subpages

    for q in questions:
        entry: dict = {
            "number": str(q.get("number", "")),
            "type": str(q.get("question_type", "short_answer")),
            "subpage_row": int(q.get("subpage_row", 1)),
            "subpage_col": int(q.get("subpage_col", 1)),
            "order_in_subpage": int(q.get("order_in_subpage", 1)),
            "max_marks": int(q.get("max_marks", 0)),
            "correct_answer": str(q.get("correct_answer") or ""),
            "text": str(q.get("question_text", "")),
            "criteria": [
                {"mark": str(c.get("mark", "")), "criterion": str(c.get("criterion", ""))}
                for c in (q.get("mark_scheme") or [])
            ],
            "options": [
                {"letter": str(o.get("letter", "")), "text": str(o.get("text", ""))}
                for o in (q.get("answer_options") or [])
            ],
            "student_answer": "",
            "assigned_marks": "",
            "explanation": "",
            # Side-channel confidence (advisory; does NOT influence marks or PDF).
            # Empty string means the AI did not provide one — downstream readers
            # treat empty as equivalent to "high".
            "confidence": "",
        }
        doc["questions"].append(entry)

    return yaml.dump(
        doc, Dumper=_MarkingDumper,
        allow_unicode=True, default_flow_style=False,
        sort_keys=False,
    )


def _yaml_questions_to_list(data: dict) -> list[dict]:
    """Convert parsed YAML blueprint dict → list of question dicts for merge logic."""
    questions = []
    for q in data.get("questions", []):
        am = q.get("assigned_marks", "")
        try:
            am_int: int | None = int(am) if str(am).strip() else None
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
            "correct_answer":   q.get("correct_answer") or None,
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
    return questions


class YamlMarkingFormat(MarkingFormat):

    # --- Blueprint construction ---

    def build_blueprint(self, page_num: int, layout, questions: list[dict]) -> str:
        return _build_yaml_blueprint(page_num, layout, questions)

    def validate_blueprint(self, text: str) -> None:
        try:
            yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Blueprint YAML is malformed: {exc}") from exc

    # --- Prompt fragments ---

    def section_A(self) -> str:
        return (
            "You are an expert exam marker. You will be shown one page of a student's exam paper "
            "and a Blueprint YAML listing every question. The blueprint is a form: each question has "
            "four empty fields for you to fill in — `student_answer`, `assigned_marks`, "
            "`explanation`, and `confidence`. Fill every field for every question in the list."
        )

    def criterion_ref(self) -> str:
        return "`criteria` entries"

    def section_C(self, rows: int, cols: int) -> str:
        return (
            "\n\nReturn ONLY the filled Blueprint YAML — no markdown fences, no surrounding text. "
            "Fill in the four empty fields in each question: "
            "`student_answer`, `assigned_marks`, `explanation`, and `confidence`. "
            "Do not change any other content.\n"
            "Use a block scalar (`|`) for `student_answer` and `explanation` "
            "so that LaTeX backslashes and braces are preserved literally.\n"
            "`assigned_marks` must be a bare integer (not a string).\n"
            "`confidence` must be one of `high`, `medium`, or `low` (plain string, no quotes needed)."
        )

    def section_D(self) -> str:
        return (
            "\n\nYAML block scalars (`|`) allow literal backslashes, `{`, `}`, `#`, `$` — "
            "no format-level escaping is needed. Write LaTeX directly.\n"
            "• LaTeX: wrap all math in $...$  "
            "(e.g. $v = 2\\pi r / T$, $3.0 \\times 10^4$ m/s, $\\frac{d}{v}$). "
            "Use \\times, \\approx, \\frac{}{}, \\pi, \\rightarrow, \\% etc. "
            "Failing to wrap math in $...$ will crash the PDF renderer.\n"
            "• Do not append a mark tally ('— X marks.') at the end of any field."
        )

    def subpage_ref(self) -> str:
        return "`subpage` entries"

    def build_user_text(self, blueprint_str: str) -> str:
        return (
            "Fill in the four empty fields (`student_answer`, `assigned_marks`, `explanation`, "
            "`confidence`) for each question:\n"
            f"{blueprint_str}"
        )

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
        result = []
        for q in questions:
            if not isinstance(q, dict):
                continue
            am = q.get("assigned_marks", "")
            try:
                am_int: int = int(am) if str(am).strip() not in ("", "null", "None") else 0
            except (ValueError, TypeError):
                am_int = 0
            result.append({
                "number":        str(q.get("number", "")),
                "subpage_row":   int(q.get("subpage_row", 1)),
                "subpage_col":   int(q.get("subpage_col", 1)),
                "assigned_marks": am_int,
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
        return yaml.dump(
            doc, Dumper=_MarkingDumper,
            allow_unicode=True, default_flow_style=False,
            sort_keys=False,
        )

    def deserialize_blueprint(self, text: str) -> dict:
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"YAML blueprint parse error: {exc}") from exc
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
