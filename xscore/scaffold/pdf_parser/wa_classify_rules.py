"""Rule-based classifiers for the writing-area detector.

Each function consumes ``_HRule`` objects whose ``consumed`` flag is False, marks
the ones it claims, and returns the resulting ``BBox`` regions.  The orchestrator
in :mod:`writing_areas` runs these in a specific order so earlier passes (table
grid, equation blank, labeled stacks) take precedence over later ones (multi-line,
short_line single / chain, similar-length cluster, inline blank).
"""

from __future__ import annotations

import re

from xscore.shared.models import BBox
from xscore.scaffold.pdf_parser.config import ParserConfig
from xscore.scaffold.pdf_parser.wa_signals import (
    _HRule,
    _MARK_BRACKET_RE,
    _VRule,
    _find_mark_indicator_near,
)


def _classify_multi_line(
    h_rules: list[_HRule],
    v_rules: list[_VRule],
    cell_width: float,
    cfg: ParserConfig,
    page_no: int,
) -> list[BBox]:
    """Group ≥2 evenly-spaced unclaimed horizontal rules into multi-line writing areas.

    Bbox height encodes the writing space; the frontend derives ``<textarea rows>``
    from bbox height ÷ typical line pitch.

    Rejects stacks crossed by ≥ 2 vertical rules — those are graph gridlines or
    table internals, not stand-alone answer lines.
    """
    free = sorted([r for r in h_rules if not r.consumed], key=lambda r: r.y)
    if len(free) < cfg.wa_lines_min_count:
        return []

    out: list[BBox] = []
    used: set[int] = set()
    for i in range(len(free)):
        if i in used:
            continue
        stack = [free[i]]
        last = free[i]
        first_pitch: float | None = None
        for j in range(i + 1, len(free)):
            if j in used:
                continue
            r = free[j]
            pitch = r.y - last.y
            if pitch <= 0:
                continue
            if abs(r.length - last.length) > max(40.0, last.length * 0.3):
                continue
            if first_pitch is None:
                if pitch > 40.0:
                    break
                first_pitch = pitch
                stack.append(r)
                last = r
                continue
            if abs(pitch - first_pitch) / first_pitch > cfg.wa_lines_pitch_tol_frac:
                break
            stack.append(r)
            last = r

        if len(stack) < cfg.wa_lines_min_count:
            continue

        avg_len = sum(r.length for r in stack) / len(stack)
        if avg_len / max(cell_width, 1.0) < cfg.wa_lines_min_column_coverage_frac:
            continue

        # Reject when ≥ 2 verticals OVERLAP the stack's y-range AND fall INSIDE
        # the stack's x-range — that's a graph gridlines block or a table internal
        # grid.  Margin guides (verticals at the very left or right of the page)
        # don't qualify because their x is outside the answer-line x range.
        y_lo = stack[0].y
        y_hi = stack[-1].y
        x_lo = min(r.x0 for r in stack)
        x_hi = max(r.x1 for r in stack)
        crossing_v = sum(
            1 for v in v_rules
            if v.y0 <= y_hi and v.y1 >= y_lo and x_lo < v.x < x_hi
        )
        if crossing_v >= 2:
            continue

        x0 = min(r.x0 for r in stack)
        x1 = max(r.x1 for r in stack)
        y0 = stack[0].y
        y1 = stack[-1].y
        pitch = (y1 - y0) / max(len(stack) - 1, 1)
        y0_pad = y0 - pitch * 0.7
        y1_pad = y1 + pitch * 0.3
        out.append(BBox(x0, y0_pad, x1, y1_pad, page_no))

        for s in stack:
            s.consumed = True
        used.update(range(i, i + len(stack)))

    return out


_LABEL_RE = re.compile(
    r"^\s*("
    r"\d+[.)]?"
    r"|statement"
    r"|explanation"
    r"|reason(?:\s+\d+)?"
    r"|name(?:\s+of\s+\w+)?"
    r"|most\s+suitable\s+\w+(?:\s+\w+)?"
    r"|advantage"
    r"|disadvantage"
    r"|comparison"
    r"|conclusion"
    r"|observation"
    r"|prediction"
    r"|method"
    r"|description"
    r"|definition"
    r"|equation"
    r"|formula"
    r")\b",
    re.I,
)


def _classify_labeled_lines(
    h_rules: list[_HRule],
    text_lines: list[tuple[float, float, float, float, str]],
    cfg: ParserConfig,
    page_no: int,
) -> list[tuple[BBox, str]]:
    """Detect labeled-line stacks and split them along label boundaries.

    Pattern: a rule whose line begins with a short prefix label (``Statement``,
    ``Explanation``, ``1``, ``2``, ``Name``, ``Reason``, etc.) is the start of an
    answer slot.  The slot continues with any *unlabeled* rules immediately below
    it.  A new label starts a fresh slot.

    Returns ``[(bbox, kind), ...]`` — kind is ``"short_line"`` for a single-rule
    slot, ``"lines"`` for a slot of ≥ 2 rules.
    """
    def label_of_line(t: str) -> str | None:
        stripped = re.sub(r"[.·•_\- ]{4,}.*$", "", t).strip()
        if not stripped:
            return None
        if len(stripped) > 40 or "?" in stripped:
            return None
        m = _LABEL_RE.match(stripped)
        if not m:
            return None
        return m.group(0).strip()

    free = [r for r in h_rules if not r.consumed]
    if not free:
        return []
    free.sort(key=lambda r: r.y)

    rule_label: dict[int, str | None] = {}
    for r in free:
        lbl: str | None = None
        for tx0, ty0, tx1, ty1, t in text_lines:
            ty_c = (ty0 + ty1) * 0.5
            if abs(ty_c - r.y) > 6:
                continue
            if tx0 >= r.x0 - 2:
                continue
            lbl = label_of_line(t)
            if lbl:
                break
        rule_label[id(r)] = lbl

    distinct_labels = {v for v in rule_label.values() if v}
    if len(distinct_labels) < 2:
        return []

    out: list[tuple[BBox, str]] = []
    i = 0
    while i < len(free):
        if rule_label[id(free[i])] is None:
            i += 1
            continue
        group = [free[i]]
        j = i + 1
        while j < len(free):
            if rule_label[id(free[j])] is not None:
                break
            if free[j].y - group[-1].y > 28:
                break
            group.append(free[j])
            j += 1

        x0 = min(r.x0 for r in group)
        x1 = max(r.x1 for r in group)
        if len(group) == 1:
            r = group[0]
            bb = BBox(r.x0, r.y - 12.0, r.x1, r.y + 4.0, page_no)
            out.append((bb, "short_line"))
        else:
            y_top = group[0].y - 14.0
            y_bot = group[-1].y + 4.0
            bb = BBox(x0, y_top, x1, y_bot, page_no)
            out.append((bb, "lines"))
        for r in group:
            r.consumed = True
        i = j
    return out


def _classify_secondary_equation_blank(
    h_rules: list[_HRule],
    text_lines: list[tuple[float, float, float, float, str]],
    cfg: ParserConfig,
    page_no: int,
) -> list[BBox]:
    """Find rules with an ``=`` text immediately to the left on the same baseline.

    Catches Cambridge stacked-answer patterns like ``t = ........`` /
    ``w = ........  [4]`` where the legacy detector only finds the line with the
    ``[n]`` suffix.
    """
    out: list[BBox] = []
    for r in h_rules:
        if r.consumed:
            continue
        for tx0, ty0, tx1, ty1, t in text_lines:
            if "=" not in t:
                continue
            ty_center = (ty0 + ty1) * 0.5
            if abs(ty_center - r.y) > 8:
                continue
            # The text line must start to the LEFT of the rule's start (label
            # comes before the answer-dots), but the line's right edge can
            # extend past r.x0 — Cambridge often renders ``t = …………`` as a
            # single text line that overlaps the rule horizontally.  Estimate
            # where ``=`` sits within the line by character position; require
            # it to be before the rule begins.
            if tx0 > r.x0 - 2:
                continue
            eq_pos_in_text = t.find("=")
            if eq_pos_in_text < 0:
                continue
            char_w = (tx1 - tx0) / max(len(t), 1)
            eq_x = tx0 + (eq_pos_in_text + 1) * char_w
            if eq_x > r.x0 + 4:
                continue
            overlay_h = cfg.wa_equation_blank_max_height_pt
            bb = BBox(r.x0, r.y - (overlay_h - 4.0), r.x1, r.y + 4.0, page_no)
            out.append(bb)
            r.consumed = True
            break
    return out


def _classify_short_line(
    h_rules: list[_HRule],
    v_rules: list[_VRule],
    text_lines: list[tuple[float, float, float, float, str]],
    cfg: ParserConfig,
    page_no: int,
) -> list[BBox]:
    """Single rule near a ``[n]`` mark indicator OR a chain of short rules on one baseline.

    - **Single**: one unclaimed horizontal rule ≥ ``wa_short_line_min_length_pt``
      long with a ``[n]`` indicator nearby.
    - **Chain**: ≥ 2 short rules sharing a y baseline (within
      ``wa_chain_blank_baseline_tol_pt``), e.g. Cambridge ``..... < ..... < .....``
      — each rule becomes its own slot.
    """
    out: list[BBox] = []
    free = [r for r in h_rules if not r.consumed]
    groups: dict[int, list[_HRule]] = {}
    bucket = max(1, int(cfg.wa_chain_blank_baseline_tol_pt))
    for r in free:
        key = int(r.y / bucket)
        groups.setdefault(key, []).append(r)
    keys_sorted = sorted(groups.keys())
    merged: list[list[_HRule]] = []
    for k in keys_sorted:
        if merged and abs(groups[k][0].y - merged[-1][0].y) <= cfg.wa_chain_blank_baseline_tol_pt:
            merged[-1].extend(groups[k])
        else:
            merged.append(list(groups[k]))

    for group in merged:
        group.sort(key=lambda r: r.x0)
        if len(group) >= 2:
            # Reject chains where the baseline y is inside ≥ 2 verticals INSIDE
            # the chain's x range — graph / table internals.
            chain_y = group[0].y
            chain_x0 = min(r.x0 for r in group)
            chain_x1 = max(r.x1 for r in group)
            crossing_v = sum(
                1 for v in v_rules
                if v.y0 - 2 <= chain_y <= v.y1 + 2 and chain_x0 < v.x < chain_x1
            )
            if crossing_v >= 2:
                continue
            for r in group:
                if r.length < cfg.wa_chain_blank_min_length_pt:
                    continue
                y0 = r.y - 12.0
                y1 = r.y + 4.0
                out.append(BBox(r.x0, y0, r.x1, y1, page_no))
                r.consumed = True
            continue
        r = group[0]
        # A single-rule answer slot needs a `[n]` indicator nearby.  Length
        # threshold is relaxed when the indicator is on the same line or the
        # next line below the rule (one line-height window) — strong signal it's
        # an answer slot, not a decorative underline — catches short slots like
        # "............ %  [3]" used for unit-suffixed answers (e.g. Q6aii
        # biology paper 32).  Outside that window we keep the strict
        # ``wa_short_line_min_length_pt`` floor to avoid picking up
        # underlined-text decorations.
        if not _find_mark_indicator_near(text_lines, r.y, r.x1, cfg):
            continue
        mark_close = any(
            _MARK_BRACKET_RE.search(t) and -8.0 <= ((ty0 + ty1) * 0.5 - r.y) <= 18.0
            for tx0, ty0, tx1, ty1, t in text_lines
        )
        min_len = (
            cfg.wa_chain_blank_min_length_pt
            if mark_close
            else cfg.wa_short_line_min_length_pt
        )
        if r.length < min_len:
            continue
        y0 = r.y - 12.0
        y1 = r.y + 4.0
        out.append(BBox(r.x0, y0, r.x1, y1, page_no))
        r.consumed = True
    return out


def _classify_inline_blank(
    h_rules: list[_HRule],
    text_lines: list[tuple[float, float, float, float, str]],
    cell_width: float,
    cfg: ParserConfig,
    page_no: int,
) -> list[BBox]:
    """Rules at the end of a text line preceded by prose → narrative fill-in-blank.

    Cambridge patterns: "Fill in the gaps" lines like ``The planets nearest the
    Sun are small and ........``, AND short labelled lines like ``calcium
    hydroxide  ............``.  Each rule is short (well under the column
    width) and preceded by a word-prefix on the same baseline.
    """
    out: list[BBox] = []
    for r in h_rules:
        if r.consumed:
            continue
        if r.length >= cell_width * 0.6:
            continue
        for tx0, ty0, tx1, ty1, t in text_lines:
            ty_c = (ty0 + ty1) * 0.5
            if abs(ty_c - r.y) > 6:
                continue
            if tx0 >= r.x0 - 2:
                continue
            prefix = re.sub(r"[.·•_\- ]{4,}.*$", "", t).strip()
            # Threshold lowered to 15 chars so labelled-line patterns like
            # ``calcium hydroxide ……`` (17 chars) pass.  Bullet markers and
            # single decorative chars like ``\x07`` are stripped first so they
            # don't pad the count artificially.
            prefix_alnum = "".join(c for c in prefix if c.isalnum() or c.isspace()).strip()
            if len(prefix_alnum) < 15:
                continue
            out.append(BBox(r.x0, r.y - 12.0, r.x1, r.y + 4.0, page_no))
            r.consumed = True
            break
    return out


def _classify_similar_length_cluster(
    h_rules: list[_HRule],
    v_rules: list[_VRule],
    cfg: ParserConfig,
    page_no: int,
) -> list[BBox]:
    """≥4 unclaimed short rules of similar length → each is an answer slot.

    Catches probability-tree, table-completion, and other diagram patterns where
    the answer dots are short (below ``wa_short_line_min_length_pt``) and have no
    adjacent ``[n]`` indicator.

    Rejected when ≥ 2 vertical rules span the cluster's vertical extent — that
    indicates a grid structure (shading grid) rather than independent slots.
    """
    free = [r for r in h_rules if not r.consumed]
    if len(free) < 3:
        return []
    y_lo = min(r.y for r in free)
    y_hi = max(r.y for r in free)
    x_lo = min(r.x0 for r in free)
    x_hi = max(r.x1 for r in free)
    crossing_v = sum(
        1 for v in v_rules
        if v.y0 <= y_hi and v.y1 >= y_lo and x_lo < v.x < x_hi
    )
    if crossing_v >= 2:
        return []
    sorted_rules = sorted(free, key=lambda r: r.length)
    best_cluster: list[_HRule] = []
    i = 0
    while i < len(sorted_rules):
        cluster = [sorted_rules[i]]
        j = i + 1
        while j < len(sorted_rules):
            if sorted_rules[j].length <= cluster[0].length * 1.25:
                cluster.append(sorted_rules[j])
                j += 1
            else:
                break
        if len(cluster) > len(best_cluster):
            best_cluster = cluster
        i = j if j > i else i + 1

    if len(best_cluster) < 3:
        return []
    out: list[BBox] = []
    for r in best_cluster:
        if r.length < cfg.wa_chain_blank_min_length_pt:
            continue
        out.append(BBox(r.x0, r.y - 12.0, r.x1, r.y + 4.0, page_no))
        r.consumed = True
    return out
