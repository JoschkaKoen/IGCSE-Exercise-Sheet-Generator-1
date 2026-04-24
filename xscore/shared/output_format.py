"""Global AI output-format selector.

Set ``AI_OUTPUT_FORMAT`` env var to ``yaml`` (default), ``json``, or ``xml``.
All three AI output surfaces (exam extraction, scheme extraction, marking)
switch together so that artifact file extensions stay consistent within a run.
"""

from __future__ import annotations

import os
from enum import Enum


class OutputFormat(Enum):
    XML  = "xml"
    YAML = "yaml"
    JSON = "json"


def get_output_format() -> OutputFormat:
    val = os.getenv("AI_OUTPUT_FORMAT", "yaml").strip().lower()
    try:
        return OutputFormat(val)
    except ValueError:
        return OutputFormat.YAML
