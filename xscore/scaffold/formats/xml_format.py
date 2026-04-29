"""XML scaffold format — pure delegation to existing scaffold_xml.py / scaffold_prompts.py.

AI_OUTPUT_FORMAT=xml routes every code path through this class so existing
behaviour is preserved bit-identically.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from xscore.scaffold.formats.base import ScaffoldFormat
from xscore.scaffold.scaffold_xml import _preprocess_xml


class XmlScaffoldFormat(ScaffoldFormat):

    def build_exam_prompt(self, layout_result, is_split: bool, n_split_pages: int) -> str:
        from xscore.scaffold.scaffold_prompts import _build_user_exam_prompt
        return _build_user_exam_prompt(layout_result, is_split, n_split_pages)

    def build_scheme_user_msg(
        self, scaffold_str: str, page_num: int, n_pages: int,
        input_label: str = "PDF",
    ) -> str:
        from xscore.prompts.loader import load_prompt
        page_note = (
            f"\n\nNote: the {input_label} you receive contains only page {page_num} of {n_pages} "
            "of the mark scheme. Only fill in correct_answer and <criterion> elements for "
            "questions whose criteria appear on this page. For all other questions leave "
            "correct_answer empty and add no <criterion> elements."
        )
        return load_prompt(
            "parse_mark_scheme_xml", section="user", scaffold=scaffold_str,
        )[1] + page_note

    def build_scheme_scaffold(self, questions: list[dict]) -> str:
        from xscore.scaffold.scaffold_xml import _build_scheme_scaffold
        return _build_scheme_scaffold(questions)

    def extract_question_numbers(self, scaffold_str: str) -> list[str]:
        try:
            root = ET.fromstring(scaffold_str)
            nums = [q.get("number", "") for q in root.findall(".//question")]
            return [n for n in nums if n]
        except ET.ParseError:
            return []

    def parse_exam_response(self, raw: str) -> tuple[list[dict], dict]:
        from xscore.scaffold.scaffold_xml import _parse_exam_xml
        return _parse_exam_xml(raw)

    def parse_scheme_response(self, raw: str) -> dict:
        from xscore.scaffold.scaffold_xml import _parse_scheme_xml
        return _parse_scheme_xml(raw)

    def serialize_exam(self, questions: list[dict], layout: dict) -> str:
        from xscore.scaffold.scaffold_xml import _serialize_exam_xml
        return _serialize_exam_xml(questions, layout)

    # ---- detect-scaffold (phase A) -----------------------------------------

    def build_scaffold_user_msg(
        self, layout_result, is_split: bool, n_split_pages: int,
    ) -> str:
        from xscore.scaffold.scaffold_prompts import _build_user_scaffold_prompt
        return _build_user_scaffold_prompt(layout_result, is_split, n_split_pages)

    def parse_scaffold_response(self, raw: str) -> tuple[list[dict], dict]:
        """Parse the detect-scaffold XML — same shape as exam XML but with
        empty ``text`` / ``answer_options`` (the model is instructed not to
        emit them). Tolerates accidental <text>/<option> elements by ignoring
        them — the fill phase produces those."""
        root = ET.fromstring(_preprocess_xml(raw))
        layout = {"rows": int(root.get("rows", 1)), "cols": int(root.get("cols", 1))}

        def _parse_q(el: ET.Element) -> dict:
            return {
                "number":        el.get("number", ""),
                "question_type": el.get("type", "short_answer"),
                "page":          int(el.get("page", 1)),
                "subpage_row":   int(el.get("subpage_row", 1)),
                "subpage_col":   int(el.get("subpage_col", 1)),
                "marks":         int(el.get("marks", 0)),
                "text":          "",
                "answer_options": [],
                "subquestions":  [_parse_q(child) for child in el.findall("question")],
            }

        return [_parse_q(q_el) for q_el in root.findall("question")], layout

    def serialize_scaffold(self, nodes: list[dict], layout: dict) -> str:
        """Serialise a clean scaffold artifact — no <text> / <option> elements."""
        def _q_el(parent: ET.Element, q: dict) -> None:
            el = ET.SubElement(parent, "question")
            el.set("number", str(q.get("number", "")))
            el.set("type", str(q.get("question_type", "short_answer")))
            el.set("page", str(q.get("page", 1)))
            el.set("subpage_row", str(q.get("subpage_row", 1)))
            el.set("subpage_col", str(q.get("subpage_col", 1)))
            el.set("marks", str(q.get("marks", 0)))
            for sub in (q.get("subquestions") or []):
                _q_el(el, sub)

        root = ET.Element("exam")
        root.set("rows", str(layout.get("rows", 1)))
        root.set("cols", str(layout.get("cols", 1)))
        for q in nodes:
            _q_el(root, q)
        ET.indent(root)
        return ET.tostring(root, encoding="unicode")

    # ---- fill (phase B) -----------------------------------------------------

    def build_fill_stub(self, filtered_nodes: list[dict]) -> str:
        """Flat list of <question number=… type=…><text></text></question>."""
        lines = []
        for n in filtered_nodes:
            num = str(n.get("number", ""))
            qt = str(n.get("question_type", "short_answer"))
            lines.append(
                f'  <question number="{num}" type="{qt}"><text></text></question>'
            )
        return "\n".join(lines)

    def build_fill_user_msg(
        self, stub_str: str, page_num: int, n_pages: int,
        expected_qnums: list[str], input_label: str = "PDF",
    ) -> str:
        from xscore.prompts.loader import load_prompt
        page_note = (
            f"\n\nNote: the {input_label} you receive contains only page {page_num} of {n_pages} "
            f"of the exam. Expected question numbers on this page: "
            + (", ".join(f'"{q}"' for q in expected_qnums) or "(none)")
            + "."
        )
        return load_prompt(
            "fill_exam_scaffold_xml", section="user", scaffold=stub_str,
        )[1] + page_note

    def parse_fill_response(self, raw: str) -> list[dict]:
        """Parse fill XML response → flat ``[{number, text, options}, …]``.

        Accepts both ``<questions>`` and ``<exam>`` roots and tolerates a flat
        list of ``<question>`` siblings with no enclosing root by wrapping when
        needed."""
        text = _preprocess_xml(raw)
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            try:
                root = ET.fromstring(f"<questions>{text}</questions>")
            except ET.ParseError as exc:
                raise RuntimeError(f"Fill XML parse error: {exc}") from exc
        out: list[dict] = []
        for q_el in root.iter("question"):
            text_el = q_el.find("text")
            options = [
                {"letter": opt.get("letter", ""), "text": (opt.text or "").strip()}
                for opt in q_el.findall("option")
            ]
            out.append({
                "number":  q_el.get("number", ""),
                "text":    (text_el.text or "").strip() if text_el is not None else "",
                "options": options,
            })
        return out

    def artifact_ext(self) -> str:
        return "xml"
