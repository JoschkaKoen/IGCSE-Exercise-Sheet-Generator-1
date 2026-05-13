"""Per-node parse / serialize helpers for the scaffold YAML shape.

Symmetric pairs:

- :func:`_parse_yaml_question` ↔ :func:`_exam_q_to_yaml_dict` — full exam
  questions (text + options).
- :func:`_parse_yaml_scaffold_node` ↔ :func:`_scaffold_node_to_yaml_dict` —
  structural scaffold nodes (text/options always empty).

Plus :func:`_mcq_default_points` — the ``MCQ_DEFAULT_POINTS`` env-var lookup
used to override ``marks=0`` on MCQ questions whose printed allocation was
missing from the source.
"""

from __future__ import annotations


def _parse_yaml_question(q: dict) -> dict:
    return {
        "number":        str(q.get("number", "")),
        "question_type": str(q.get("type", "short_answer")),
        "page":          int(q.get("page", 1)),
        "subpage_row":   int(q.get("subpage_row", 1)),
        "subpage_col":   int(q.get("subpage_col", 1)),
        "marks":         int(q.get("marks", 0)),
        "text":          str(q.get("text", "")).strip(),
        "answer_options": [
            {"letter": str(o.get("letter", "")), "text": str(o.get("text", "")).strip()}
            for o in (q.get("options") or [])
            if isinstance(o, dict)
        ],
        "subquestions": [
            _parse_yaml_question(s) for s in (q.get("subquestions") or [])
            if isinstance(s, dict)
        ],
    }


def _mcq_default_points() -> int:
    """Audit item [68]: override marks=0 for MCQ questions to this value.

    Cambridge papers usually print "[1]" next to MCQ stems but some omit it;
    the AI extracts marks=0 in those cases, silently dropping the question
    from per-question totals. Setting MCQ_DEFAULT_POINTS=N forces marks=N for
    any MCQ where the AI returned 0. Set to 0 in env to disable the override.
    """
    import os
    raw = os.environ.get("MCQ_DEFAULT_POINTS", "1").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 1


def _parse_yaml_scaffold_node(q: dict) -> dict:
    """Parse a detect-scaffold YAML node — same shape as the exam parser but
    text/options are forced empty (the model is instructed not to emit them;
    this defends against accidental emission)."""
    qtype = str(q.get("type", "short_answer"))
    marks = int(q.get("marks", 0))
    if qtype == "multiple_choice" and marks == 0:
        marks = _mcq_default_points()
    return {
        "number":        str(q.get("number", "")),
        "question_type": qtype,
        "page":          int(q.get("page", 1)),
        "subpage_row":   int(q.get("subpage_row", 1)),
        "subpage_col":   int(q.get("subpage_col", 1)),
        "marks":         marks,
        "text":          "",
        "answer_options": [],
        "subquestions": [
            _parse_yaml_scaffold_node(s) for s in (q.get("subquestions") or [])
            if isinstance(s, dict)
        ],
    }


def _scaffold_node_to_yaml_dict(q: dict) -> dict:
    """Serialise a scaffold node — drops text/options for a clean artifact."""
    entry: dict = {
        "number":      str(q.get("number", "")),
        "type":        str(q.get("question_type", "short_answer")),
        "page":        int(q.get("page", 1)),
        "subpage_row": int(q.get("subpage_row", 1)),
        "subpage_col": int(q.get("subpage_col", 1)),
        "marks":       int(q.get("marks", 0)),
    }
    subs = q.get("subquestions") or []
    if subs:
        entry["subquestions"] = [_scaffold_node_to_yaml_dict(s) for s in subs]
    return entry


def _exam_q_to_yaml_dict(q: dict) -> dict:
    entry: dict = {
        "number":      str(q.get("number", "")),
        "type":        str(q.get("question_type", "short_answer")),
        "page":        int(q.get("page", 1)),
        "subpage_row": int(q.get("subpage_row", 1)),
        "subpage_col": int(q.get("subpage_col", 1)),
        "marks":       int(q.get("marks", 0)),
        "text":        str(q.get("text", "")),
    }
    opts = q.get("answer_options") or []
    if opts:
        entry["options"] = [
            {"letter": str(o.get("letter", "")), "text": str(o.get("text", ""))}
            for o in opts
        ]
    subs = q.get("subquestions") or []
    if subs:
        entry["subquestions"] = [_exam_q_to_yaml_dict(s) for s in subs]
    return entry
