"""Factory for the active marking format.

Call ``get_marking_format()`` once per pipeline step and reuse the instance.
"""

from __future__ import annotations

from xscore.marking.formats.base import FormatParseError, MarkingFormat
from xscore.shared.output_format import OutputFormat, get_output_format


def get_marking_format() -> MarkingFormat:
    """Return a MarkingFormat instance for the active AI_OUTPUT_FORMAT."""
    fmt = get_output_format()
    if fmt == OutputFormat.XML:
        from xscore.marking.formats.xml_format import XmlMarkingFormat
        return XmlMarkingFormat()
    if fmt == OutputFormat.JSON:
        from xscore.marking.formats.json_format import JsonMarkingFormat
        return JsonMarkingFormat()
    from xscore.marking.formats.yaml_format import YamlMarkingFormat
    return YamlMarkingFormat()


__all__ = ["get_marking_format", "MarkingFormat", "FormatParseError"]
