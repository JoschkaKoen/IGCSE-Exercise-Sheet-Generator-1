"""Custom YAML dumper for marking responses.

LaTeX content in ``student_answer`` and ``explanation`` needs to round-trip
without escape-character mangling. Force literal block scalars (``|``) for any
string containing newlines or backslashes; plain scalars otherwise.
"""

from __future__ import annotations

import yaml


class _MarkingDumper(yaml.SafeDumper):
    """SafeDumper subclass with a LaTeX-friendly string representer.

    Subclassing isolates the representer registration so it doesn't leak to
    any other ``yaml.dump`` caller in the process.
    """


def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    if "\n" in data or "\\" in data:
        # Strip per-line trailing whitespace so PyYAML can use block-scalar
        # style. Without this, multiline strings with trailing whitespace fall
        # back to double-quoted form, which interprets backslashes as escapes
        # and silently destroys LaTeX commands.
        data = "\n".join(line.rstrip() for line in data.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_MarkingDumper.add_representer(str, _str_representer)
