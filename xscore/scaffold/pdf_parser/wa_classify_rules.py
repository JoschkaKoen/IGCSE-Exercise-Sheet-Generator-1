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
from xscore.scaffold.pdf_parser.wa_geometry import (
    bbox_for_equation_blank,
    bbox_for_short_line,
    verticals_crossing_at_y,
    verticals_crossing_range,
)
from xscore.scaffold.pdf_parser.wa_signals import (
    _HRule,
    _MARK_BRACKET_RE,
    _VRule,
    _find_mark_indicator_near,
)


# File-local constants lifted from inline literals (Phase 1 refactor).
# Calibration history lives in git; group here for discoverability.

# _classify_multi_line:
_MULTI_LINE_MAX_INITIAL_PITCH_PT = 40.0   # break if first pair pitch exceeds this
# Minimum pitch between adjacent rules in a `lines` stack.  Cambridge answer
# lines are always ≥ 12pt apart (one body-text line height).  Rules with
# tighter spacing (3-8pt) are decorative (graph axes with parallel arrow
# lines — mathematics paper 12 Q18b x-axis, physics_51 page-margin border)
# and must not be promoted to a multi-line answer area.
_MULTI_LINE_MIN_INITIAL_PITCH_PT = 10.0
_MULTI_LINE_LENGTH_TOL_ABS_PT = 40.0      # absolute length tolerance between adjacent rules
_MULTI_LINE_LENGTH_TOL_FRAC = 0.30        # fractional length tolerance
_MULTI_LINE_PAD_TOP_FRAC = 0.7            # bbox top padding = pitch * this
_MULTI_LINE_PAD_BOT_FRAC = 0.3            # bbox bot padding = pitch * this
# Looser column-coverage floor for stacks of ≥ 3 rules — that pattern is
# unambiguous evidence of an answer area even when the slot is narrow
# (figure-side answer slots like a_level_biology specimen p5).
_MULTI_LINE_TIGHT_COVERAGE_MIN = 0.08

# _classify_labeled_lines:
_LABELED_LINE_BASELINE_TOL_PT = 6.0       # |ty_c - r.y| ≤ tol to bind label to rule
_LABELED_LINE_MAX_GAP_PT = 28.0           # gap between consecutive rules in a labeled stack
_LABELED_LINES_PAD_TOP_PT = 14.0          # multi-rule labeled bbox top padding
_LABELED_LINES_PAD_BOT_PT = 4.0           # multi-rule labeled bbox bottom padding
# Labeled-rule clustering: only consider a labeled rule as part of a real
# stacked answer if there's another labeled rule within this y-gap.  Single
# uppercase labels (A, B, P, Q, R) can fire on figure-callout leader lines
# (a photo's "A" arrow into the image); requiring a near-neighbour rejects
# solitary callouts while keeping legitimate stacks intact (Q1b's
# adaptation→explanation gaps run ~52-78pt; callout-to-slot gaps run ≥200pt).
_LABELED_LINES_CLUSTER_MAX_GAP_PT = 80.0

# _classify_secondary_equation_blank:
_EQ_BLANK_EQ_POSITION_SLACK_PT = 4.0      # eq_x must be within (rule.x0 + slack)

# _classify_short_line:
_SHORT_LINE_MARK_ABOVE_PT = 8.0           # mark indicator within (-above_pt, +below_pt) of rule.y
_SHORT_LINE_MARK_BELOW_PT = 18.0

# _classify_similar_length_cluster:
_SIMILAR_CLUSTER_LENGTH_TOL_FRAC = 1.25   # next-rule length ≤ first * this stays in cluster
_SIMILAR_CLUSTER_MIN_SIZE = 3             # cluster must have ≥ this many rules

# _classify_inline_blank:
_INLINE_BLANK_MAX_LENGTH_FRAC = 0.6       # skip rules that span ≥ this fraction of cell_width
_INLINE_BLANK_BASELINE_TOL_PT = 6.0       # baseline-match tolerance between text and rule
_INLINE_BLANK_MIN_PREFIX_CHARS = 15       # alphanumeric prefix length before the dots


def _classify_multi_line(
    h_rules: list[_HRule],
    v_rules: list[_VRule],
    cell_width: float,
    cfg: ParserConfig,
    page_no: int,
) -> list[tuple[BBox, str]]:
    """Group ≥2 evenly-spaced unclaimed horizontal rules into multi-line writing areas.

    Bbox height encodes the writing space; the frontend derives ``<textarea rows>``
    from bbox height ÷ typical line pitch.

    Rejects stacks crossed by ≥ 2 vertical rules — those are graph gridlines or
    table internals, not stand-alone answer lines.
    """
    free = sorted([r for r in h_rules if not r.consumed], key=lambda r: r.y)
    if len(free) < cfg.wa_lines_min_count:
        return []

    # Pre-compute: for each rule, does it have a SIBLING rule at the same y
    # baseline but different x (a horizontal-chain partner)?  Such rules are
    # part of a row-of-pairs pattern (e.g. coordinate-answer pairs
    # ``( x , y )``); they should be handled by the chain logic in
    # ``_classify_short_line``, not grouped vertically into a ``lines``
    # region.  (mathematics_43 Q9a: 3 ``( x , y )`` rows where multi_line
    # was merging the 3 left coordinates into one tall ``lines`` region.)
    chain_baseline_tol = cfg.wa_chain_blank_baseline_tol_pt
    has_sibling = set()
    for r in free:
        for s in free:
            if s is r:
                continue
            if abs(s.y - r.y) <= chain_baseline_tol:
                # Sibling found at same baseline — must be horizontally
                # separated (not overlapping)
                if s.x1 < r.x0 - 2 or s.x0 > r.x1 + 2:
                    has_sibling.add(id(r))
                    break

    out: list[tuple[BBox, str]] = []
    used: set[int] = set()
    for i in range(len(free)):
        if i in used:
            continue
        # Skip rules that are part of a horizontal chain — let
        # _classify_short_line's chain pass handle them.
        if id(free[i]) in has_sibling:
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
            if abs(r.length - last.length) > max(
                _MULTI_LINE_LENGTH_TOL_ABS_PT, last.length * _MULTI_LINE_LENGTH_TOL_FRAC
            ):
                continue
            if first_pitch is None:
                if pitch > _MULTI_LINE_MAX_INITIAL_PITCH_PT:
                    break
                if pitch < _MULTI_LINE_MIN_INITIAL_PITCH_PT:
                    # Too tight to be answer lines — graph axis / arrow lines
                    # in a figure (mathematics paper 12 Q18b: x-axis + arrow
                    # line at ~3pt pitch).  Stop grouping from this anchor.
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
        # Column coverage check is meant to filter out spurious short-rule
        # stacks (decorative dashes, etc.).  Relax the threshold when the
        # stack has ≥ 3 rules at consistent pitch — that pattern is
        # unambiguous evidence of a multi-line answer area, even when the
        # rule is short because the slot sits beside a figure (a_level_biology
        # specimen p5: 3-line dotted slots next to the sugar-test diagrams).
        if len(stack) >= 3:
            min_cov = _MULTI_LINE_TIGHT_COVERAGE_MIN
        else:
            min_cov = cfg.wa_lines_min_column_coverage_frac
        if avg_len / max(cell_width, 1.0) < min_cov:
            continue

        # Reject when ≥ 2 verticals OVERLAP the stack's y-range AND fall INSIDE
        # the stack's x-range — that's a graph gridlines block or a table internal
        # grid.  Margin guides (verticals at the very left or right of the page)
        # don't qualify because their x is outside the answer-line x range.
        y_lo = stack[0].y
        y_hi = stack[-1].y
        x_lo = min(r.x0 for r in stack)
        x_hi = max(r.x1 for r in stack)
        crossing_v = verticals_crossing_range(v_rules, x_lo, x_hi, y_lo, y_hi)
        if crossing_v >= 2:
            continue

        x0 = min(r.x0 for r in stack)
        x1 = max(r.x1 for r in stack)
        y0 = stack[0].y
        y1 = stack[-1].y
        pitch = (y1 - y0) / max(len(stack) - 1, 1)
        y0_pad = y0 - pitch * _MULTI_LINE_PAD_TOP_FRAC
        y1_pad = y1 + pitch * _MULTI_LINE_PAD_BOT_FRAC
        out.append((BBox(x0, y0_pad, x1, y1_pad, page_no), "lines"))

        for s in stack:
            s.consumed = True
        used.update(range(i, i + len(stack)))

    return out


# Maximum length of the prefix-label substring (after stripping trailing
# filler dots).  Cambridge labels run from single-character ("A", "1") to
# ~25-char phrases ("Password to test rule 4", "Most suitable measuring
# cylinder").  Anything longer is question prose, not a label.
_LABEL_MAX_CHARS = 30
_LABEL_MAX_WORDS = 5


def _label_prefix(t: str) -> str | None:
    """Return the prefix-label text from a line of the form ``<label> ...... [n]``.

    A label is any short identifier (≤ _LABEL_MAX_CHARS chars, ≤ _LABEL_MAX_WORDS
    words) sitting at the start of a line before a run of filler glyphs.
    Examples that match: "Reason:", "Password to test rule 1", "A", "B",
    "ball Q", "Closest to the Sun", "improvement", "difficulty", "x_G", "F".

    Rejects:
    - Full sentences (ending in period)
    - Lines containing a question mark (those are the question text itself)
    - Pure-symbol prefixes (no alpha characters — e.g. "[4]" mark indicators)
    - Empty prefixes (the whole line was filler)
    """
    # Strip trailing filler-glyph run and everything after it.  Cambridge uses
    # ".", "·", "•", "_", and "-" as filler chars; ≥ 4 in a row signals the
    # start of the answer-dots area.
    stripped = re.sub(r"[.·•_\-]{4,}.*$", "", t).strip()
    if not stripped:
        return None
    # Strip a trailing colon — labels like "Reason:" should normalise to
    # "Reason" so two "Reason:" lines (different occurrences of the same
    # label word) count as one distinct label.
    stripped_no_colon = stripped.rstrip(":").rstrip()
    if not stripped_no_colon:
        return None
    if len(stripped_no_colon) > _LABEL_MAX_CHARS:
        return None
    if "?" in stripped_no_colon:
        return None
    # Reject if the prefix looks like a full sentence (ends in period and is
    # multi-word — exclude single-letter abbreviations like "A.").
    if stripped_no_colon.endswith(".") and len(stripped_no_colon.split()) > 1:
        return None
    words = stripped_no_colon.split()
    if not words or len(words) > _LABEL_MAX_WORDS:
        return None
    # Require at least one alphabetical character — reject pure numerics
    # that aren't structured labels (e.g. "= 9.81" remnants).
    if not any(c.isalpha() for c in stripped_no_colon):
        # Allow pure-numeric IF it's a structured label like "1.", "2)", "3"
        if not re.fullmatch(r"\d+[.)]?", stripped_no_colon):
            return None
    return stripped_no_colon


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
        return _label_prefix(t)

    free = [r for r in h_rules if not r.consumed]
    if not free:
        return []
    free.sort(key=lambda r: r.y)

    rule_label: dict[int, str | None] = {}
    for r in free:
        lbl: str | None = None
        for tx0, ty0, tx1, ty1, t in text_lines:
            ty_c = (ty0 + ty1) * 0.5
            if abs(ty_c - r.y) > _LABELED_LINE_BASELINE_TOL_PT:
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

    # Filter out solitary labeled rules — a labeled rule is "valid" only if
    # another labeled rule sits within _LABELED_LINES_CLUSTER_MAX_GAP_PT pt
    # in y.  This rejects photo-callout labels that happen to align with
    # unrelated answer-slot labels far down the page (a_level_biology_23
    # Q1ai: "A" leader-line at y=150 vs A/B answer slots at y=402/428).
    labeled_rules = [r for r in free if rule_label[id(r)] is not None]
    valid_label_ids: set[int] = set()
    for r in labeled_rules:
        for other in labeled_rules:
            if other is r:
                continue
            if abs(other.y - r.y) <= _LABELED_LINES_CLUSTER_MAX_GAP_PT:
                valid_label_ids.add(id(r))
                break

    # Re-check distinct-labels constraint after filtering.
    remaining_labels = {rule_label[id(r)] for r in labeled_rules if id(r) in valid_label_ids}
    if len(remaining_labels) < 2:
        return []

    out: list[tuple[BBox, str]] = []
    i = 0
    while i < len(free):
        # Treat solitary labeled rules as unlabeled (they don't start a group
        # but can still be absorbed into a neighbouring group).
        if rule_label[id(free[i])] is None or id(free[i]) not in valid_label_ids:
            i += 1
            continue
        group = [free[i]]
        j = i + 1
        while j < len(free):
            # Only break on a VALID labeled rule — solitary callout labels
            # were filtered out above and should not split a real group.
            if id(free[j]) in valid_label_ids:
                break
            if free[j].y - group[-1].y > _LABELED_LINE_MAX_GAP_PT:
                break
            group.append(free[j])
            j += 1

        x0 = min(r.x0 for r in group)
        x1 = max(r.x1 for r in group)
        if len(group) == 1:
            r = group[0]
            out.append((bbox_for_short_line(r, page_no, cfg), "short_line"))
        else:
            y_top = group[0].y - _LABELED_LINES_PAD_TOP_PT
            y_bot = group[-1].y + _LABELED_LINES_PAD_BOT_PT
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
) -> list[tuple[BBox, str]]:
    """Find rules with an ``=`` text immediately to the left on the same baseline.

    Catches Cambridge stacked-answer patterns like ``t = ........`` /
    ``w = ........  [4]`` where the legacy detector only finds the line with the
    ``[n]`` suffix.
    """
    out: list[tuple[BBox, str]] = []
    for r in h_rules:
        if r.consumed:
            continue
        for tx0, ty0, tx1, ty1, t in text_lines:
            if "=" not in t:
                continue
            ty_center = (ty0 + ty1) * 0.5
            if abs(ty_center - r.y) > cfg.wa_eq_blank_baseline_tol_pt:
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
            if eq_x > r.x0 + _EQ_BLANK_EQ_POSITION_SLACK_PT:
                continue
            out.append((bbox_for_equation_blank(r, page_no, cfg), "equation_blank"))
            r.consumed = True
            break
    return out


def _classify_short_line(
    h_rules: list[_HRule],
    v_rules: list[_VRule],
    text_lines: list[tuple[float, float, float, float, str]],
    cfg: ParserConfig,
    page_no: int,
) -> list[tuple[BBox, str]]:
    """Single rule near a ``[n]`` mark indicator OR a chain of short rules on one baseline.

    - **Single**: one unclaimed horizontal rule ≥ ``wa_short_line_min_length_pt``
      long with a ``[n]`` indicator nearby.
    - **Chain**: ≥ 2 short rules sharing a y baseline (within
      ``wa_chain_blank_baseline_tol_pt``), e.g. Cambridge ``..... < ..... < .....``
      — each rule becomes its own slot.
    """
    out: list[tuple[BBox, str]] = []
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
            crossing_v = verticals_crossing_at_y(
                v_rules, chain_x0, chain_x1, chain_y,
                y_pad=cfg.wa_chain_blank_baseline_tol_pt,
                x_strict=True,
            )
            if crossing_v >= 2:
                continue
            for r in group:
                if r.length < cfg.wa_chain_blank_min_length_pt:
                    continue
                out.append((bbox_for_short_line(r, page_no, cfg), "short_line"))
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
        # Dotted text rules ARE the answer-line dots themselves — give them
        # a wider below-the-rule indicator window because Cambridge sometimes
        # places the ``[n]`` indicator a line below an intervening figure
        # caption (a_level_biology_23 Q5bi: RNA-sequence dots at y=511, [1]
        # at y=550 — 39pt below, beyond the default 36pt proximity).
        extra_below = 12.0 if r.dotted else 0.0
        if not _find_mark_indicator_near(
            text_lines, r.y, r.x1, cfg, extra_below_tol_pt=extra_below
        ):
            continue
        mark_close = any(
            _MARK_BRACKET_RE.search(t)
            and -_SHORT_LINE_MARK_ABOVE_PT
            <= ((ty0 + ty1) * 0.5 - r.y)
            <= _SHORT_LINE_MARK_BELOW_PT
            for tx0, ty0, tx1, ty1, t in text_lines
        )
        min_len = (
            cfg.wa_chain_blank_min_length_pt
            if mark_close
            else cfg.wa_short_line_min_length_pt
        )
        if r.length < min_len:
            continue
        out.append((bbox_for_short_line(r, page_no, cfg), "short_line"))
        r.consumed = True
    return out


def _classify_inline_blank(
    h_rules: list[_HRule],
    text_lines: list[tuple[float, float, float, float, str]],
    cell_width: float,
    cfg: ParserConfig,
    page_no: int,
) -> list[tuple[BBox, str]]:
    """Rules at the end of a text line preceded by prose → narrative fill-in-blank.

    Cambridge patterns: "Fill in the gaps" lines like ``The planets nearest the
    Sun are small and ........``, AND short labelled lines like ``calcium
    hydroxide  ............``.  Each rule is short (well under the column
    width) and preceded by a word-prefix on the same baseline.
    """
    out: list[tuple[BBox, str]] = []
    for r in h_rules:
        if r.consumed:
            continue
        if r.length >= cell_width * _INLINE_BLANK_MAX_LENGTH_FRAC:
            continue
        for tx0, ty0, tx1, ty1, t in text_lines:
            ty_c = (ty0 + ty1) * 0.5
            if abs(ty_c - r.y) > _INLINE_BLANK_BASELINE_TOL_PT:
                continue
            if tx0 >= r.x0 - 2:
                continue
            prefix = re.sub(r"[.·•_\- ]{4,}.*$", "", t).strip()
            # Threshold lowered to 15 chars so labelled-line patterns like
            # ``calcium hydroxide ……`` (17 chars) pass.  Bullet markers and
            # single decorative chars like ``\x07`` are stripped first so they
            # don't pad the count artificially.
            prefix_alnum = "".join(c for c in prefix if c.isalnum() or c.isspace()).strip()
            if len(prefix_alnum) < _INLINE_BLANK_MIN_PREFIX_CHARS:
                continue
            # The prefix must read as English prose — at least
            # ``wa_inline_blank_min_prefix_chars`` of it must be ASCII
            # alphanumerics or spaces.  Without this gate, barcode-area
            # text-runs (sequences of non-ASCII font-tricks like
            # ``ĬÙĊ®Ġ´íÈõ``) match the threshold and produce tiny
            # bottom-of-page false positives on every Cambridge math /
            # physics / chemistry page that prints a QR code.
            ascii_prefix = "".join(
                c for c in prefix_alnum if c.isascii() and (c.isalnum() or c.isspace())
            )
            if len(ascii_prefix) < _INLINE_BLANK_MIN_PREFIX_CHARS:
                continue
            out.append((bbox_for_short_line(r, page_no, cfg), "short_line"))
            r.consumed = True
            break
    return out


def _classify_similar_length_cluster(
    h_rules: list[_HRule],
    v_rules: list[_VRule],
    cfg: ParserConfig,
    page_no: int,
) -> list[tuple[BBox, str]]:
    """≥4 unclaimed short rules of similar length → each is an answer slot.

    Catches probability-tree, table-completion, and other diagram patterns where
    the answer dots are short (below ``wa_short_line_min_length_pt``) and have no
    adjacent ``[n]`` indicator.

    Rejected when ≥ 2 vertical rules span the cluster's vertical extent — that
    indicates a grid structure (shading grid) rather than independent slots.
    """
    free = [r for r in h_rules if not r.consumed]
    if len(free) < _SIMILAR_CLUSTER_MIN_SIZE:
        return []
    y_lo = min(r.y for r in free)
    y_hi = max(r.y for r in free)
    x_lo = min(r.x0 for r in free)
    x_hi = max(r.x1 for r in free)
    if verticals_crossing_range(v_rules, x_lo, x_hi, y_lo, y_hi) >= 2:
        return []
    sorted_rules = sorted(free, key=lambda r: r.length)
    best_cluster: list[_HRule] = []
    i = 0
    while i < len(sorted_rules):
        cluster = [sorted_rules[i]]
        j = i + 1
        while j < len(sorted_rules):
            if sorted_rules[j].length <= cluster[0].length * _SIMILAR_CLUSTER_LENGTH_TOL_FRAC:
                cluster.append(sorted_rules[j])
                j += 1
            else:
                break
        if len(cluster) > len(best_cluster):
            best_cluster = cluster
        i = j if j > i else i + 1

    if len(best_cluster) < _SIMILAR_CLUSTER_MIN_SIZE:
        return []
    out: list[tuple[BBox, str]] = []
    for r in best_cluster:
        if r.length < cfg.wa_chain_blank_min_length_pt:
            continue
        out.append((bbox_for_short_line(r, page_no, cfg), "short_line"))
        r.consumed = True
    return out
