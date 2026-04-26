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

    # --- Prompt fragments ---

    def prompt_name(self) -> str:
        return "ai_marking_xml"

    def criterion_ref(self) -> str:
        return "<criterion> elements"

    def subpage_ref(self) -> str:
        return "<subpage> elements"

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
        return _blueprint_xml_to_dict(text)

    def artifact_ext(self) -> str:
        return "xml"
