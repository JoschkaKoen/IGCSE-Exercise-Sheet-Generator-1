"""AI marking output format — YAML.

YAML block scalars (``|``) preserve literal backslashes, ``{``, ``}``, ``#``, ``$``,
so LaTeX content in student_answer and explanation needs no format-level escaping.
"""

from __future__ import annotations

import yaml

from xscore.shared.response_parsing import strip_code_fences as _strip_fences


class FormatParseError(ValueError):
    """Raised by parse_response() on malformed AI output.

    Callers catch this and ``break`` (no retry).
    """


class MarkingFailure(Exception):
    """Raised when all retry attempts to mark a page are exhausted."""
    def __init__(self, *, attempts: int, last_exc: BaseException, last_raw: str = "") -> None:
        super().__init__(f"All {attempts} marking attempts failed: {last_exc}")
        self.attempts = attempts
        self.last_exc = last_exc
        self.last_raw = last_raw


def parse_confidence_int(value: object) -> int:
    """Parse a confidence value to int in [0, 10]; default 5 on missing/unparseable.

    The AI is instructed to emit an integer 0–10. Anything else (None, empty
    string, stale ``"low"`` / ``"medium"`` / ``"high"`` from a pre-change run)
    falls through to the mid-band default — no string→int compat shim.
    """
    if value is None:
        return 5
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 5
    if n < 0:
        return 0
    if n > 10:
        return 10
    return n


def parse_problem(value: object) -> str:
    """Parse a problem value to a stripped string; default ``""`` on missing."""
    if value is None:
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# Custom YAML dumper — uses literal block scalars for strings with backslashes
# or newlines; plain scalars otherwise.
# ---------------------------------------------------------------------------

class _MarkingDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    if "\n" in data or "\\" in data:
        # Strip per-line trailing whitespace so PyYAML can use block-scalar
        # style. Without this, multiline strings with trailing whitespace fall
        # back to double-quoted form, which interprets backslashes as escapes
        # and silently destroys LaTeX commands.
        data = "\n".join(line.rstrip() for line in data.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_MarkingDumper.add_representer(str, _str_representer)


def _build_yaml_blueprint(page_num: int, layout, questions: list[dict]) -> str:
    """Build a YAML blueprint string for one exam page."""
    doc: dict = {"page": page_num}
    if layout.rows > 1 or layout.cols > 1:
        doc["layout"] = {"rows": layout.rows, "cols": layout.cols}
        subpages = []
        for r in range(1, layout.rows + 1):
            for c in range(1, layout.cols + 1):
                from xscore.marking.blueprints import _quadrant_label
                subpages.append({
                    "row": r, "col": c,
                    "label": _quadrant_label(r, c, layout.rows, layout.cols),
                })
        doc["subpages"] = subpages
    doc["questions"] = []

    from xscore.marking.blueprints import _clean_text
    for q in questions:
        entry: dict = {
            "number": str(q.get("number", "")),
            "type": str(q.get("question_type", "short_answer")),
            "max_marks": int(q.get("max_marks", 0)),
        }
        if layout.rows > 1 or layout.cols > 1:
            entry["subpage_row"] = int(q.get("subpage_row", 1))
            entry["subpage_col"] = int(q.get("subpage_col", 1))
            entry["order_in_subpage"] = int(q.get("order_in_subpage", 1))
        entry["question_text"] = _clean_text(str(q.get("question_text", "")))

        _options = [
            {"letter": str(o.get("letter", "")), "text": _clean_text(str(o.get("text", "")))}
            for o in (q.get("answer_options") or [])
        ]
        if _options:
            entry["options"] = _options

        _criteria = [
            {"mark": str(c.get("mark", "")), "criterion": _clean_text(str(c.get("criterion", "")))}
            for c in (q.get("mark_scheme") or [])
        ]
        if _criteria:
            entry["criteria"] = _criteria

        _ca = str(q.get("correct_answer") or "")
        if _ca:
            entry["correct_answer"] = _ca

        # AI-target placeholders — always emitted, even when empty, so the
        # AI sees a fixed contract of slots to fill. confidence is parsed
        # back as int 0–10 (default 5 on missing/unparseable); problem is
        # a short freeform string, default "".
        entry["student_answer"] = ""
        entry["assigned_marks"] = ""
        entry["explanation"] = ""
        entry["confidence"] = ""
        entry["problem"] = ""
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
            # YAML null becomes Python None → str(None) == "None"; treat the
            # literal "null"/"None" strings as absent too.
            am_int: int | None = int(am) if str(am).strip() not in ("", "null", "None") else None
        except (ValueError, TypeError):
            am_int = None
        questions.append({
            "number":           str(q.get("number", "")),
            "question_type":    str(q.get("type", "short_answer")),
            "subpage_row":      int(q.get("subpage_row", 1)),
            "subpage_col":      int(q.get("subpage_col", 1)),
            "order_in_subpage": int(q.get("order_in_subpage", 1)),
            "question_text":    str(q.get("question_text", "")),
            "answer_options":   [
                {"letter": str(o.get("letter", "")), "text": str(o.get("text", ""))}
                for o in (q.get("options") or [])
            ],
            # str() wrap: model occasionally emits unquoted YAML int (e.g. `correct_answer: 5`); parser must coerce.
            "correct_answer":   str(q.get("correct_answer") or "").strip() or None,
            "max_marks":        int(q.get("max_marks", 0)),
            "mark_scheme":      [
                {"mark": str(c.get("mark", "")), "criterion": str(c.get("criterion", ""))}
                for c in (q.get("criteria") or [])
            ],
            "student_answer":   str(q.get("student_answer") or "").strip(),
            "assigned_marks":   am_int,
            "explanation":      str(q.get("explanation") or "").strip(),
            "confidence":       parse_confidence_int(q.get("confidence")),
            "problem":          parse_problem(q.get("problem")),
        })
    return questions


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
