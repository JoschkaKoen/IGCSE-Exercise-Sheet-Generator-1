"""Backwards-compat shim for the historical monolithic ``formats.base`` module.

The actual implementations now live in dedicated sibling modules (mirroring
the :mod:`xscore.scaffold.formats` layout):

- :mod:`._yaml_io` — :class:`_MarkingDumper` and the LaTeX-friendly
  ``_str_representer``.
- :mod:`._parsers` — :class:`FormatParseError`, :class:`MarkingFailure`,
  :func:`parse_confidence_int`, :func:`parse_problem`, and the
  ``_yaml_questions_to_list`` helper used by ``deserialize_blueprint``.
- :mod:`._prompt_builders` — :func:`_build_yaml_blueprint`.
- :mod:`.marking_format` — the :class:`MarkingFormat` class.

This shim keeps the historic ``from xscore.marking.formats.base import …``
call sites working (e.g. ``xscore.marking.ai_mark`` imports ``FormatParseError``
and ``MarkingFailure`` directly from here).
"""

from __future__ import annotations

from xscore.marking.formats._parsers import (  # noqa: F401
    FormatParseError,
    MarkingFailure,
    _yaml_questions_to_list,
    parse_confidence_int,
    parse_problem,
)
from xscore.marking.formats._prompt_builders import (  # noqa: F401
    _build_yaml_blueprint,
)
from xscore.marking.formats._yaml_io import (  # noqa: F401
    _MarkingDumper,
    _str_representer,
)
from xscore.marking.formats.marking_format import MarkingFormat  # noqa: F401
