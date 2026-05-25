"""Question-number normalisation — shared between scaffold and marking.

Strips parentheses from raw question numbers so that ``"7(a)"`` and ``"7a"``
compare equal as dict keys. Used by scaffold (qtree walks, page assignment,
scheme lookup, graphic detection) and by marking (per-question report tables
and scheme-graphic insertion).

Pure stdlib — depends only on ``re``.
"""

from __future__ import annotations

import re


def norm_qnum(s: str) -> str:
    """Return *s* with all ``(`` and ``)`` removed.

    Used as the canonical hashable key for question lookups: ``"7(a)"`` →
    ``"7a"``. Both forms appear in raw exam data depending on layout; the
    normalised form is the single source of truth for dict keys.
    """
    return re.sub(r"[()]", "", s)
