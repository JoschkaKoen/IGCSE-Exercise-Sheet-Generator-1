"""Per-field parsers and helpers for marking responses.

Shared between :class:`~xscore.marking.formats.marking_format.MarkingFormat` and
its prompt builders. The two error classes (:class:`FormatParseError`,
:class:`MarkingFailure`) live here too — they're the format-layer exception
contract.
"""

from __future__ import annotations


class FormatParseError(ValueError):
    """Raised by ``MarkingFormat.parse_response`` on malformed AI output.

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
