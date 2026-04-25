"""Factory for the active scaffold format.

Call ``get_scaffold_format()`` once per pipeline invocation and pass the
instance to ``_do_exam_call``, ``detect_scheme_graphics``, and
``parse_mark_scheme_pages``.
"""

from __future__ import annotations

from xscore.scaffold.formats.base import ScaffoldFormat
from xscore.shared.output_format import OutputFormat, get_output_format


def get_scaffold_format() -> ScaffoldFormat:
    """Return a ScaffoldFormat instance for the active AI_OUTPUT_FORMAT."""
    fmt = get_output_format()
    if fmt == OutputFormat.XML:
        from xscore.scaffold.formats.xml_format import XmlScaffoldFormat
        return XmlScaffoldFormat()
    if fmt == OutputFormat.JSON:
        from xscore.scaffold.formats.json_format import JsonScaffoldFormat
        return JsonScaffoldFormat()
    from xscore.scaffold.formats.yaml_format import YamlScaffoldFormat
    return YamlScaffoldFormat()


__all__ = ["get_scaffold_format", "ScaffoldFormat"]
