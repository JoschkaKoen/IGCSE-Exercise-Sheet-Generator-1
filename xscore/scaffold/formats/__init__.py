"""Factory for the active scaffold format.

Call ``get_scaffold_format()`` once per pipeline invocation and pass the
instance to ``detect_exam_scaffold``, ``fill_exam_scaffold``,
``detect_scheme_graphics``, and ``parse_mark_scheme_pages``.
"""

from __future__ import annotations

from pathlib import Path

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


def load_exam_questions_artifact(path: Path) -> dict:
    """Load a serialized ``exam_questions.{yaml|json|xml}`` artifact.

    Returns ``{rows, cols, questions}`` shaped like the YAML form regardless of
    the underlying format, so callers don't need to branch on the active
    ``ALL_AI_OUTPUT_FORMAT``. Returns ``{}`` if the file does not exist.
    """
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(text) or {}
    if suffix == ".json":
        import json
        return json.loads(text) or {}
    if suffix == ".xml":
        from xscore.scaffold.formats.xml_format import XmlScaffoldFormat
        questions, layout = XmlScaffoldFormat().parse_exam_response(text)
        return {
            "rows":      layout.get("rows", 1),
            "cols":      layout.get("cols", 1),
            "questions": questions,
        }
    raise ValueError(f"Unknown exam_questions format: {path.suffix!r}")


__all__ = ["get_scaffold_format", "ScaffoldFormat", "load_exam_questions_artifact"]
