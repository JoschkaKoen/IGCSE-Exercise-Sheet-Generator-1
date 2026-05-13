"""Backwards-compat shim for the historical monolithic ``formats.base`` module.

The actual implementations now live in dedicated sibling modules:

- :mod:`._yaml_io` — the custom :class:`yaml.SafeDumper` subclass and the
  recovering scheme-YAML loader.
- :mod:`._prompt_builders` — the four ``_build_user_*`` / ``_common_tail_*``
  user-prompt builders.
- :mod:`._parsers` — per-node parse / serialise helpers and the
  ``MCQ_DEFAULT_POINTS`` lookup.
- :mod:`.scaffold_format` — the :class:`ScaffoldFormat` class.

This shim keeps the historic ``from xscore.scaffold.formats.base import …``
call sites working (e.g. ``scaffold_xml.py``, ``scaffold_fill.py``,
``scaffold_graphics.py`` import ``ScaffoldFormat`` or ``_mcq_default_points``
directly from here).
"""

from __future__ import annotations

from xscore.scaffold.formats._parsers import (  # noqa: F401
    _exam_q_to_yaml_dict,
    _mcq_default_points,
    _parse_yaml_question,
    _parse_yaml_scaffold_node,
    _scaffold_node_to_yaml_dict,
)
from xscore.scaffold.formats._prompt_builders import (  # noqa: F401
    _build_user_exam_prompt_yaml,
    _build_user_question_numbers_prompt_yaml,
    _common_tail_scaffold_yaml,
    _common_tail_yaml,
)
from xscore.scaffold.formats._yaml_io import (  # noqa: F401
    _PLAIN_KV_RE,
    _DQ_KV_RE,
    _QUOTE_INDICATORS,
    _ScaffoldDumper,
    _double_quoted_to_single,
    _load_scheme_yaml_recovering,
    _quote_unquoted_value,
    _str_representer,
)
from xscore.scaffold.formats.scaffold_format import ScaffoldFormat  # noqa: F401
