"""XML scaffold format — pure delegation to existing scaffold_xml.py / scaffold_prompts.py.

AI_OUTPUT_FORMAT=xml routes every code path through this class so existing
behaviour is preserved bit-identically.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from xscore.scaffold.formats.base import ScaffoldFormat


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

    def artifact_ext(self) -> str:
        return "xml"
