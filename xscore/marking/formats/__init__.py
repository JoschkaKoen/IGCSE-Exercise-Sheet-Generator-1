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


__all__ = [
    "get_marking_format", "MarkingFormat", "FormatParseError",
    "parse_confidence_int", "parse_problem",
]
