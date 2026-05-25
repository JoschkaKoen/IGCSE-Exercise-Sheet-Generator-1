"""Pure geometric helpers for the writing-area detector.

No PyMuPDF dependency.  Functions here are stateless, side-effect-free, and
deterministic so the per-classifier logic in :mod:`wa_classify_grid` and
:mod:`wa_classify_rules` can stay focused on rule-pattern decisions.

Imported by the classifier modules and by :mod:`writing_areas`.  Does NOT
import from :mod:`wa_classify_grid` / :mod:`wa_classify_rules` /
:mod:`writing_areas` — one-way dependency, no cycle.
"""

from __future__ import annotations

from xscore.shared.models import BBox
from xscore.scaffold.pdf_parser.config import ParserConfig
from xscore.scaffold.pdf_parser.wa_signals import _HRule, _VRule


def bbox_for_short_line(rule: _HRule, page_no: int, cfg: ParserConfig) -> BBox:
    """Overlay bbox shape used by short_line / labeled-line single / chain /
    inline_blank / similar-length-cluster classifiers.

    Tight rectangle around the horizontal rule:
    ``rule.y - wa_bbox_pad_above_short_pt`` (top) to
    ``rule.y + wa_bbox_pad_below_short_pt`` (bottom).
    """
    return BBox(
        rule.x0,
        rule.y - cfg.wa_bbox_pad_above_short_pt,
        rule.x1,
        rule.y + cfg.wa_bbox_pad_below_short_pt,
        page_no,
    )


def bbox_for_equation_blank(rule: _HRule, page_no: int, cfg: ParserConfig) -> BBox:
    """Overlay bbox shape for an equation-blank slot bound to a horizontal rule.

    Height is ``wa_equation_blank_max_height_pt`` (capped tighter than the
    legacy structural-anchor padding); bottom is ``rule.y +
    wa_bbox_pad_below_short_pt``; top is derived so the total height matches
    ``wa_equation_blank_max_height_pt``.
    """
    overlay_h = cfg.wa_equation_blank_max_height_pt
    pad_below = cfg.wa_bbox_pad_below_short_pt
    return BBox(
        rule.x0,
        rule.y - (overlay_h - pad_below),
        rule.x1,
        rule.y + pad_below,
        page_no,
    )


def rules_within_dy(rules: list[_HRule], y: float, tol: float) -> list[_HRule]:
    """Horizontal rules whose y-coordinate is within ``tol`` of ``y``."""
    return [r for r in rules if abs(r.y - y) <= tol]


def border_coverage(rule_lo: float, rule_hi: float, cell_lo: float, cell_hi: float) -> float:
    """Fraction of ``[cell_lo, cell_hi]`` covered by ``[rule_lo, rule_hi]``.

    Returns 0.0 when the cell has zero width (avoids ZeroDivisionError).  Used
    by ``_classify_table_grid`` to score each of the four border sides of a
    candidate cell.
    """
    span = cell_hi - cell_lo
    if span <= 0:
        return 0.0
    overlap = max(0.0, min(rule_hi, cell_hi) - max(rule_lo, cell_lo))
    return overlap / span


def verticals_crossing_range(
    v_rules: list[_VRule],
    x_lo: float,
    x_hi: float,
    y_lo: float,
    y_hi: float,
) -> int:
    """Count verticals that span the ``[y_lo, y_hi]`` y-range AND fall strictly
    inside the ``(x_lo, x_hi)`` x-range.

    Used by ``_classify_multi_line`` and ``_classify_similar_length_cluster``
    to reject candidate stacks that overlap a graph grid or table internal.
    """
    return sum(
        1 for v in v_rules
        if v.y0 <= y_hi and v.y1 >= y_lo and x_lo < v.x < x_hi
    )


def verticals_crossing_at_y(
    v_rules: list[_VRule],
    x_lo: float,
    x_hi: float,
    y: float,
    *,
    y_pad: float = 0.0,
    x_strict: bool = True,
) -> int:
    """Count verticals whose y-extent covers ``y`` (within ``y_pad``) and whose
    x-coordinate lies inside the x-range.

    Two callers, two modes:
    - ``_classify_short_line`` chain guard: ``y_pad=2``, ``x_strict=True``
      (strict ``x_lo < v.x < x_hi``).
    - ``_consume_rules_in_graph_grid``: ``y_pad=2``, ``x_strict=False``
      (padded inclusive ``x_lo - 2 <= v.x <= x_hi + 2``).
    """
    if x_strict:
        return sum(
            1 for v in v_rules
            if v.y0 - y_pad <= y <= v.y1 + y_pad and x_lo < v.x < x_hi
        )
    return sum(
        1 for v in v_rules
        if v.y0 - y_pad <= y <= v.y1 + y_pad and x_lo - 2 <= v.x <= x_hi + 2
    )
