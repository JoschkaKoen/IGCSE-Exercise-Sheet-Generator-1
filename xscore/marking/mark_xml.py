"""XML serialisation and parsing for AI marking blueprints.

No PDF, threading, or API dependencies — fully isolated, independently testable.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from xscore.marking.formats import parse_confidence_int, parse_problem


def filled_to_xml(filled: dict) -> str:
    """Serialise a filled marking blueprint dict to Blueprint XML.

    Using the in-memory ``filled`` dict (rather than the raw AI response)
    guarantees every question is present even if the model omitted some.
    LaTeX content is stored verbatim as element text — no JSON escaping layer.
    """
    layout = filled.get("layout") or {}
    root = ET.Element("marking")
    root.set("page", str(filled.get("page", "")))
    root.set("rows", str(layout.get("rows", 1)))
    root.set("cols", str(layout.get("cols", 1)))
    root.set("student_name", str(filled.get("student_name") or ""))

    for q in filled.get("questions") or []:
        qel = ET.SubElement(root, "question")
        qel.set("number", str(q.get("number", "")))
        qel.set("type", str(q.get("question_type", "")))
        qel.set("subpage_row", str(q.get("subpage_row", 1)))
        qel.set("subpage_col", str(q.get("subpage_col", 1)))
        qel.set("order_in_subpage", str(q.get("order_in_subpage", 1)))
        qel.set("max_marks", str(q.get("max_marks", 0)))
        qel.set("correct_answer", str(q.get("correct_answer") or ""))

        text_el = ET.SubElement(qel, "text")
        text_el.text = str(q.get("question_text") or "")

        for crit in q.get("mark_scheme") or []:
            crit_el = ET.SubElement(qel, "criterion")
            crit_el.set("mark", str(crit.get("mark") or ""))
            crit_el.text = str(crit.get("criterion") or "")

        for opt in q.get("answer_options") or []:
            opt_el = ET.SubElement(qel, "option")
            opt_el.set("letter", str(opt.get("letter") or ""))
            opt_el.text = str(opt.get("text") or "")

        sa_el = ET.SubElement(qel, "student_answer")
        sa_el.text = str(q.get("student_answer") or "")

        am_el = ET.SubElement(qel, "assigned_marks")
        am_val = q.get("assigned_marks")
        am_el.text = str(am_val) if am_val is not None else ""

        exp_el = ET.SubElement(qel, "explanation")
        exp_el.text = str(q.get("explanation") or "")

        # Side-channel signals (advisory; never affect marks or PDFs).
        # confidence is an int 0–10; written as the bare digits. Empty
        # element when the source dict has no value — downstream parser
        # defaults to 5. problem is a short freeform string; empty
        # element means "no problem flagged".
        conf_el = ET.SubElement(qel, "confidence")
        cf_val = q.get("confidence")
        conf_el.text = str(cf_val) if cf_val is not None else ""
        prob_el = ET.SubElement(qel, "problem")
        prob_el.text = str(q.get("problem") or "")

    ET.indent(root)
    return ET.tostring(root, encoding="unicode")


class MarkingFailure(Exception):
    """Raised when all retry attempts to mark a page are exhausted."""
    def __init__(self, *, attempts: int, last_exc: BaseException, last_raw: str = "") -> None:
        super().__init__(f"All {attempts} marking attempts failed: {last_exc}")
        self.attempts = attempts
        self.last_exc = last_exc
        self.last_raw = last_raw


def _repair_mismatched_leaf_tags(raw: str) -> str:
    """Fix the observed model error: leaf element closed with the wrong sibling tag.

    e.g. <explanation>long text</student_answer> → <explanation>long text</explanation>
    Applied per <question> block to avoid cross-question interference.
    """
    _LEAF = ('student_answer', 'assigned_marks', 'explanation', 'confidence', 'problem')

    def _fix_within_question(q_text: str) -> str:
        for tag in _LEAF:
            for wrong in _LEAF:
                if wrong == tag:
                    continue
                q_text = re.sub(
                    r'(<' + tag + r'(?:\s[^>]*)?>)(.*?)</' + wrong + r'>',
                    r'\1\2</' + tag + r'>',
                    q_text,
                    flags=re.DOTALL,
                )
        return q_text

    return re.sub(
        r'(<question\b[^>]*>)(.*?)(</question>)',
        lambda m: m.group(1) + _fix_within_question(m.group(2)) + m.group(3),
        raw,
        flags=re.DOTALL,
    )


def _parse_xml_response(raw: str) -> list[dict]:
    """Parse the AI's XML marking response into a list of question dicts."""
    from xscore.shared.response_parsing import strip_code_fences
    raw = strip_code_fences(raw)
    # Extract the <marking>…</marking> block, discarding any surrounding
    # reasoning text or stray duplicate </marking> tags the model may emit.
    m = re.search(r'(<marking\b.*?</marking>)', raw, re.DOTALL)
    if m:
        raw = m.group(1)
    # Replace HTML <br> variants with a space (not valid XML void elements)
    raw = re.sub(r'<br\s*/?>', ' ', raw, flags=re.IGNORECASE)
    # Fix unescaped & in element text (e.g. student wrote "P & Q")
    raw = re.sub(r'&(?![a-zA-Z#]\w*;)', '&amp;', raw)
    # Fix bare < in text content (e.g. "x < y", "< 50%") — leave valid tag starts intact
    raw = re.sub(r'<(?!/?[a-zA-Z_:!?])', '&lt;', raw)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raw = _repair_mismatched_leaf_tags(raw)
        root = ET.fromstring(raw)  # raises ET.ParseError if still malformed
    questions = []
    for q in root.findall('question'):
        sa_el = q.find('student_answer')
        am_el = q.find('assigned_marks')
        re_el = q.find('explanation')
        cf_el = q.find('confidence')
        pr_el = q.find('problem')
        # assigned_marks: prefer child element (new format), fall back to attribute (legacy)
        if am_el is not None and (am_el.text or '').strip():
            try:
                assigned_marks = int(am_el.text.strip())
            except ValueError:
                assigned_marks = 0
        else:
            assigned_marks = int(q.get('assigned_marks', 0))
        questions.append({
            'number':         q.get('number', ''),
            'subpage_row':    int(q.get('subpage_row', 1)),
            'subpage_col':    int(q.get('subpage_col', 1)),
            'assigned_marks': assigned_marks,
            'student_answer': (sa_el.text or '').strip() if sa_el is not None else '',
            'explanation':    (re_el.text or '').strip() if re_el is not None else '',
            'confidence':     parse_confidence_int(cf_el.text) if cf_el is not None else 5,
            'problem':        parse_problem(pr_el.text) if pr_el is not None else '',
        })
    return questions


def _blueprint_xml_to_dict(xml_str: str) -> dict:
    """Parse <marking page rows cols> XML into the blueprint dict format."""
    root = ET.fromstring(xml_str)
    questions = []
    for qel in root.findall("question"):
        text_el = qel.find("text")
        answer_options = [
            {"letter": o.get("letter", ""), "text": (o.text or "").strip()}
            for o in qel.findall("option")
        ]
        mark_scheme = [
            {"mark": c.get("mark", ""), "criterion": (c.text or "").strip()}
            for c in qel.findall("criterion")
            if (c.text or "").strip()
        ]
        sa_el = qel.find("student_answer")
        am_el = qel.find("assigned_marks")
        ex_el = qel.find("explanation")
        cf_el = qel.find("confidence")
        pr_el = qel.find("problem")
        questions.append({
            "number":          qel.get("number", ""),
            "question_type":   qel.get("type", "short_answer"),
            "subpage_row":     int(qel.get("subpage_row", 1)),
            "subpage_col":     int(qel.get("subpage_col", 1)),
            "order_in_subpage": int(qel.get("order_in_subpage", 1)),
            "question_text":   (text_el.text or "").strip() if text_el is not None else "",
            "answer_options":  answer_options,
            "correct_answer":  qel.get("correct_answer") or None,
            "max_marks":       int(qel.get("max_marks", 0)),
            "mark_scheme":     mark_scheme,
            "student_answer":  (sa_el.text or "").strip() if sa_el is not None else "",
            "assigned_marks":  None if am_el is None or not (am_el.text or "").strip() else int(am_el.text),
            "explanation":     (ex_el.text or "").strip() if ex_el is not None else "",
            "confidence":      parse_confidence_int(cf_el.text) if cf_el is not None else 5,
            "problem":         parse_problem(pr_el.text) if pr_el is not None else "",
        })
    result: dict = {
        "page":     int(root.get("page", 1)),
        "layout":   {"rows": int(root.get("rows", 1)), "cols": int(root.get("cols", 1))},
        "questions": questions,
    }
    student_name = root.get("student_name") or ""
    if student_name:
        result["student_name"] = student_name
    return result
