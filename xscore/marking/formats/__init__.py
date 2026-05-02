"""Factory for the marking format.

Call ``get_marking_format()`` once per pipeline step and reuse the instance.
"""

from __future__ import annotations

from xscore.marking.formats.base import (
    FormatParseError,
    MarkingFailure,
    MarkingFormat,
)


def get_marking_format() -> MarkingFormat:
    return MarkingFormat()


__all__ = [
    "get_marking_format",
    "MarkingFormat",
    "FormatParseError",
    "MarkingFailure",
]
