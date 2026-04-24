"""XML marking format — pure delegation to existing mark_xml.py functions.

AI_OUTPUT_FORMAT=xml routes every code path through this class.
No logic is duplicated; all functions are called exactly as the pre-migration
code called them, guaranteeing bit-identical output.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from xscore.marking.formats.base import FormatParseError, MarkingFormat


class XmlMarkingFormat(MarkingFormat):

    # --- Blueprint construction ---

    def build_blueprint(self, page_num: int, layout, questions: list[dict]) -> str:
        from xscore.marking.blueprints import _build_blueprint_xml
        return _build_blueprint_xml(page_num, layout, questions)

    def validate_blueprint(self, text: str) -> None:
        try:
            ET.fromstring(text)
        except ET.ParseError as exc:
            raise RuntimeError(f"Blueprint XML is malformed: {exc}") from exc

    # --- Prompt fragments (verbatim copies of mark_page.py pre-migration text) ---

    def section_A(self) -> str:
        return (
            "You are an expert exam marker. You will be shown one page of a student's exam paper "
            "and a Blueprint XML listing every question. The blueprint is a form: each question has "
            "three empty fields for you to fill in — <student_answer>, <assigned_marks>, and "
            "<explanation>. Fill every field for every question in the list."
        )

    def criterion_ref(self) -> str:
        return "<criterion> elements"

    def section_C(self, rows: int, cols: int) -> str:
        return (
            "\n\nReturn ONLY the filled Blueprint XML — no markdown fences, no surrounding text. "
            "Fill in the three empty XML fields in each <question>: "
            "<student_answer>, <assigned_marks>, and <explanation>. "
            "Do not change any other content.\n"
            "CRITICAL — each element must be closed with its own matching tag. "
            "WRONG: <explanation>text</student_answer>. "
            "RIGHT: <explanation>text</explanation>. "
            "Never close <explanation> with </student_answer> or vice versa."
        )

    def section_D(self) -> str:
        return (
            "\n\nXML validity:\n"
            "• In element text use &lt; for <, &gt; for >, &amp; for &.\n"
            "• Do not use HTML tags (e.g. <br>) — use a space or comma instead.\n"
            "• LaTeX: wrap all math in $...$  "
            "(e.g. $v = 2\\pi r / T$, $3.0 \\times 10^4$ m/s, $\\frac{d}{v}$). "
            "Use \\times, \\approx, \\frac{}{}, \\pi, \\rightarrow, \\% etc. "
            "Failing to wrap math in $...$ will crash the PDF renderer.\n"
            "• Do not append a mark tally ('— X marks.') at the end of any field."
        )

    def subpage_ref(self) -> str:
        return "<subpage> elements"

    def build_user_text(self, blueprint_str: str) -> str:
        return (
            "Fill in the three empty fields for each question "
            "(<student_answer>, <assigned_marks>, <explanation>):\n"
            f"{blueprint_str}"
        )

    # --- API enforcement ---

    def api_extra_kwargs(self, model: str) -> dict:
        return {}

    def prefer_stream(self) -> bool:
        return True

    # --- Response parsing ---

    def parse_response(self, raw: str) -> list[dict]:
        from xscore.marking.mark_xml import _parse_xml_response
        try:
            return _parse_xml_response(raw)
        except ET.ParseError as exc:
            raise FormatParseError(f"XML: {exc}") from exc

    # --- Serialisation ---

    def serialize_filled(self, filled: dict) -> str:
        from xscore.marking.mark_xml import filled_to_xml
        return filled_to_xml(filled)

    def deserialize_blueprint(self, text: str) -> dict:
        from xscore.marking.mark_xml import _blueprint_xml_to_dict
        result = _blueprint_xml_to_dict(text)
        try:
            root = ET.fromstring(text)
            student_name = root.get("student_name") or ""
            if student_name:
                result["student_name"] = student_name
        except ET.ParseError:
            pass
        return result

    def artifact_ext(self) -> str:
        return "xml"
