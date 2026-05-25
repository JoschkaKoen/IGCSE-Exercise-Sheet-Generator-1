"""Prompt-blueprint builders for the AI marking step.

Single public function: :func:`_build_yaml_blueprint`, called by
:class:`~xscore.marking.formats.marking_format.MarkingFormat.build_blueprint`.
Kept separate from :class:`MarkingFormat` so the blueprint shape can evolve
without touching the format class.
"""

from __future__ import annotations

import yaml

from xscore.marking.formats._yaml_io import _MarkingDumper


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
