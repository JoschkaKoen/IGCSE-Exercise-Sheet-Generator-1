"""Unified per-leaf detection of answer-region geometry.

Five kinds, classified in order so each pass claims its rule segments:

- ``table_cell``  — empty cell inside a vector-bordered table grid.
- ``equation_blank`` — Cambridge ``label = …… [n]`` slot, defined by a text pattern.
- ``lines``       — stack of ≥2 evenly-spaced horizontal rules (multi-line writing area).
- ``short_line``  — single horizontal rule near a ``[n]`` mark indicator.
- ``box``         — explicit outlined rectangle, or large empty band with no rules.

The detector consumes vector paths from ``page.get_drawings()`` plus text-rendered
dotted "rules" (runs of ``.`` glyphs on a shared baseline), and runs strictly within
the leaf's ``q.bbox`` clipped to its layout cell.
"""

from __future__ import annotations

from dataclasses import dataclass

import fitz

from xscore.shared.models import BBox, Question, WritingArea
from xscore.scaffold.pdf_parser.answer_fields import infer_equation_blank_bboxes
from xscore.scaffold.pdf_parser.config import ParserConfig
from xscore.scaffold.pdf_parser.layout import cell_for_point
from xscore.scaffold.pdf_parser.regions import clip_horizontal_bounds


@dataclass
class _HRule:
    """A horizontal rule segment in source-PDF coordinates."""

    y: float
    x0: float
    x1: float
    dotted: bool
    consumed: bool = False

    @property
    def length(self) -> float:
        return self.x1 - self.x0


@dataclass
class _VRule:
    x: float
    y0: float
    y1: float
    consumed: bool = False

    @property
    def length(self) -> float:
        return self.y1 - self.y0


@dataclass
class _ClosedRect:
    x0: float
    y0: float
    x1: float
    y1: float
    consumed: bool = False


def _segments_from_drawing(item: tuple) -> list[tuple[float, float, float, float]]:
    """Extract straight-line endpoint pairs from a single ``get_drawings`` item.

    Returns ``[(x0, y0, x1, y1), ...]``.  Handles ``("l", p0, p1)`` line ops and
    ``("re", rect)`` rect ops (each rect yields its four sides).
    """
    op = item[0]
    if op == "l" and len(item) >= 3:
        p0, p1 = item[1], item[2]
        return [(float(p0.x), float(p0.y), float(p1.x), float(p1.y))]
    if op == "re" and len(item) >= 2:
        r = item[1]
        x0, y0, x1, y1 = float(r.x0), float(r.y0), float(r.x1), float(r.y1)
        return [
            (x0, y0, x1, y0),  # top
            (x1, y0, x1, y1),  # right
            (x0, y1, x1, y1),  # bottom
            (x0, y0, x0, y1),  # left
        ]
    return []


def _is_rect_drawing(d: dict) -> tuple[float, float, float, float] | None:
    """Return the rectangle (x0, y0, x1, y1) if *d* is a single closed rectangle path.

    Detects both ``("re", rect)`` items and four-side closed paths.
    """
    items = d.get("items") or []
    # Common case: one ``re`` op.
    for it in items:
        if it[0] == "re" and len(it) >= 2:
            r = it[1]
            return (float(r.x0), float(r.y0), float(r.x1), float(r.y1))
    # Closed 4-side path: gather segments and check for one rectangular shape.
    segs: list[tuple[float, float, float, float]] = []
    for it in items:
        segs.extend(_segments_from_drawing(it))
    if len(segs) != 4:
        return None
    horiz = [s for s in segs if abs(s[1] - s[3]) < 0.5]
    vert = [s for s in segs if abs(s[0] - s[2]) < 0.5]
    if len(horiz) != 2 or len(vert) != 2:
        return None
    xs = sorted({round(s[0], 1) for s in segs} | {round(s[2], 1) for s in segs})
    ys = sorted({round(s[1], 1) for s in segs} | {round(s[3], 1) for s in segs})
    if len(xs) != 2 or len(ys) != 2:
        return None
    return (xs[0], ys[0], xs[1], ys[1])


def _extract_vector_segments(
    page: fitz.Page, clip: fitz.Rect, cfg: ParserConfig
) -> tuple[list[_HRule], list[_VRule], list[_ClosedRect]]:
    """Collect horizontal + vertical line segments and explicit closed rectangles.

    Segments are filtered to those intersecting *clip*.  Closed rectangles whose
    width or height are below ``wa_table_cell_min_side_pt`` are discarded as noise.
    """
    h_segs: list[tuple[float, float, float, float]] = []
    v_segs: list[tuple[float, float, float, float]] = []
    rects: list[_ClosedRect] = []

    for d in page.get_drawings():
        r = d.get("rect")
        if r is not None and not fitz.Rect(r).intersects(clip):
            continue
        # Identify explicit rectangles (stroked outlines).
        rect_xy = _is_rect_drawing(d)
        if rect_xy is not None:
            x0, y0, x1, y1 = rect_xy
            w, h = x1 - x0, y1 - y0
            if w >= cfg.wa_table_cell_min_side_pt and h >= cfg.wa_table_cell_min_side_pt:
                rects.append(_ClosedRect(x0=x0, y0=y0, x1=x1, y1=y1))
            # Even when treated as a rectangle, still emit its sides as line segments
            # so the table-grid pass can stitch rectangles together with adjacent rules.
        items = d.get("items") or []
        for it in items:
            for sx0, sy0, sx1, sy1 in _segments_from_drawing(it):
                dx = abs(sx1 - sx0)
                dy = abs(sy1 - sy0)
                # Horizontal: dy small relative to dx, and meaningful length.
                if dy <= cfg.wa_h_rule_max_height_pt and dx >= cfg.wa_h_rule_min_length_pt:
                    x0, x1 = min(sx0, sx1), max(sx0, sx1)
                    y = (sy0 + sy1) * 0.5
                    h_segs.append((y, x0, x1, 0.0))  # dotted=0 (we'll override later)
                # Vertical: dx small relative to dy.
                elif dx <= cfg.wa_v_rule_max_width_pt and dy >= cfg.wa_v_rule_min_length_pt:
                    y0, y1 = min(sy0, sy1), max(sy0, sy1)
                    x = (sx0 + sx1) * 0.5
                    v_segs.append((x, y0, y1, 0.0))

    # Filter to clip
    def _h_in_clip(s: tuple[float, float, float, float]) -> bool:
        y, x0, x1, _ = s
        return clip.y0 - 1 <= y <= clip.y1 + 1 and not (x1 < clip.x0 or x0 > clip.x1)

    def _v_in_clip(s: tuple[float, float, float, float]) -> bool:
        x, y0, y1, _ = s
        return clip.x0 - 1 <= x <= clip.x1 + 1 and not (y1 < clip.y0 or y0 > clip.y1)

    h_segs = [s for s in h_segs if _h_in_clip(s)]
    v_segs = [s for s in v_segs if _v_in_clip(s)]

    # Cluster colinear horizontals by y (merging dashed segments into one logical rule).
    h_rules = _cluster_horizontals(h_segs, cfg.wa_rule_cluster_y_tol_pt)
    v_rules = _cluster_verticals(v_segs, cfg.wa_rule_cluster_x_tol_pt)

    # Filter rects to clip.
    rects = [
        rc for rc in rects
        if not (rc.x1 < clip.x0 or rc.x0 > clip.x1 or rc.y1 < clip.y0 or rc.y0 > clip.y1)
    ]
    return h_rules, v_rules, rects


def _cluster_horizontals(
    segs: list[tuple[float, float, float, float]], y_tol: float
) -> list[_HRule]:
    """Merge segments sharing a y-coordinate (within tolerance) into one rule each.

    Dashed/dotted rules render as many short segments at the same y; this collapses
    them so the classifier sees a single logical rule with the full x-extent.
    """
    if not segs:
        return []
    segs = sorted(segs, key=lambda s: s[0])
    clusters: list[list[tuple[float, float, float, float]]] = [[segs[0]]]
    for s in segs[1:]:
        if abs(s[0] - clusters[-1][-1][0]) <= y_tol:
            clusters[-1].append(s)
        else:
            clusters.append([s])

    out: list[_HRule] = []
    for cluster in clusters:
        y = sum(s[0] for s in cluster) / len(cluster)
        x0 = min(s[1] for s in cluster)
        x1 = max(s[2] for s in cluster)
        # Dotted if many short segments at this y.
        spans = [(s[1], s[2]) for s in cluster]
        avg_span = (sum(s[1] - s[0] for s in spans) / len(spans)) if spans else 0.0
        dotted = len(cluster) >= 4 and avg_span < 6.0
        out.append(_HRule(y=y, x0=x0, x1=x1, dotted=dotted))
    return out


def _cluster_verticals(
    segs: list[tuple[float, float, float, float]], x_tol: float
) -> list[_VRule]:
    if not segs:
        return []
    segs = sorted(segs, key=lambda s: s[0])
    clusters: list[list[tuple[float, float, float, float]]] = [[segs[0]]]
    for s in segs[1:]:
        if abs(s[0] - clusters[-1][-1][0]) <= x_tol:
            clusters[-1].append(s)
        else:
            clusters.append([s])

    out: list[_VRule] = []
    for cluster in clusters:
        x = sum(s[0] for s in cluster) / len(cluster)
        y0 = min(s[1] for s in cluster)
        y1 = max(s[2] for s in cluster)
        out.append(_VRule(x=x, y0=y0, y1=y1))
    return out


def _extract_text_dotted_rules(
    page: fitz.Page, clip: fitz.Rect, cfg: ParserConfig
) -> list[_HRule]:
    """Synthesize horizontal rules from "filler glyph" runs on a single baseline.

    Cambridge renders answer-line dots in two patterns we have to handle:

    1. A long run of the same character — e.g. ``.................`` or
       ``ĭĭĭĭĭĭĭĭĭĭĭ`` (some embedded fonts map the dot glyph to an exotic codepoint
       like U+012D — see Q21 on the 0580 March 2025 paper).
    2. Spaced-out dots like ``. . . . . . . . . .``, where the dots and the spaces
       between them alternate, often as one-character-per-span runs.

    Algorithm: walk each line's text, find every maximal run of length
    ≥ ``wa_dotted_text_min_run`` consisting of *one repeated non-alphanumeric
    "filler" character* (optionally interspersed with whitespace).  Emit one
    ``_HRule`` per match, with the x-extent measured from the contributing
    characters' span bboxes.
    """
    import re

    rules: list[_HRule] = []

    def is_filler(c: str) -> bool:
        if c.isspace():
            return False
        # Only ASCII letters/digits are "real" content; exotic Unicode chars (e.g.
        # ``ĭ`` U+012D used by some Cambridge fonts as a dot substitute) count as
        # fillers if they're part of a long repeated run on a baseline.
        if c.isascii() and c.isalnum():
            return False
        if c in "()[]{}<>,;:!?\"'`":
            return False
        return True

    # Use rawdict so each *character* has its own glyph bbox.  The dict format only
    # gives span bboxes, and linear interpolation across a span containing both wide
    # letters and narrow dot glyphs places dot indices many points left of where the
    # dots actually start (proportional-font issue).
    for block in page.get_text("rawdict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            bb = line["bbox"]
            if not fitz.Rect(bb).intersects(clip):
                continue
            spans = line["spans"]
            if not spans:
                continue
            joined: list[str] = []
            char_x0: list[float] = []
            char_x1: list[float] = []
            for sp in spans:
                for ch_info in sp.get("chars") or []:
                    c = ch_info.get("c", "")
                    bbox = ch_info.get("bbox")
                    if not c or bbox is None:
                        continue
                    joined.append(c)
                    char_x0.append(float(bbox[0]))
                    char_x1.append(float(bbox[2]))
            line_text = "".join(joined)
            if len(line_text) < cfg.wa_dotted_text_min_run:
                continue

            # Walk the line, find runs where one non-alphanumeric filler char repeats
            # (with optional whitespace) ≥ wa_dotted_text_min_run times.
            i = 0
            while i < len(line_text):
                c = line_text[i]
                if not is_filler(c):
                    i += 1
                    continue
                # Greedy: extend as long as we see this same filler char or whitespace.
                j = i + 1
                count = 1
                while j < len(line_text):
                    nxt = line_text[j]
                    if nxt == c:
                        count += 1
                        j += 1
                    elif nxt.isspace():
                        j += 1
                    else:
                        break
                if count >= cfg.wa_dotted_text_min_run:
                    start = i
                    end = j - 1
                    while end > start and line_text[end].isspace():
                        end -= 1
                    if start < len(char_x0) and end < len(char_x1):
                        x0 = char_x0[start]
                        x1 = char_x1[end]
                        if x1 > x0:
                            y = (float(bb[1]) + float(bb[3])) * 0.5
                            rules.append(_HRule(y=y, x0=x0, x1=x1, dotted=True))
                i = j
    return rules


def _text_lines_in(
    page: fitz.Page, clip: fitz.Rect
) -> list[tuple[float, float, float, float, str]]:
    """All text lines intersecting *clip*: ``(x0, y0, x1, y1, text)``."""
    out: list[tuple[float, float, float, float, str]] = []
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        for line in block["lines"]:
            bb = line["bbox"]
            r = fitz.Rect(bb)
            if not r.intersects(clip):
                continue
            text = "".join(s["text"] for s in line["spans"]).strip()
            if not text:
                continue
            out.append(
                (float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3]), text)
            )
    return out


def _classify_table_grid(
    h_rules: list[_HRule],
    v_rules: list[_VRule],
    rects: list[_ClosedRect],
    text_lines: list[tuple[float, float, float, float, str]],
    cfg: ParserConfig,
    page_no: int,
    page: fitz.Page | None = None,
) -> list[BBox]:
    """Find empty cells inside vector-bordered tables.

    Cluster the rules into a grid: for every adjacent pair of horizontals × every
    adjacent pair of verticals, the (h_i, h_{i+1}, v_j, v_{j+1}) rectangle is a cell
    candidate.  A candidate is kept when ≥ ``wa_table_border_completeness_min`` of
    its perimeter is covered by detected rules / closed-rect sides, and no text
    span overlaps it (headers and pre-filled cells contain text, so they're excluded).

    Tables are treated all-or-nothing: if any empty-looking cell in a discovered
    table can't be safely confirmed, the whole table is dropped (the leaf falls back
    to bottom-form per §2 of the plan).
    """
    if len(h_rules) < 2 or len(v_rules) < 2:
        return []

    # Sort by coordinate.
    hs = sorted(h_rules, key=lambda r: r.y)
    vs = sorted(v_rules, key=lambda r: r.x)

    cells: list[tuple[BBox, _HRule, _HRule, _VRule, _VRule]] = []
    for i in range(len(hs) - 1):
        for j in range(len(vs) - 1):
            h_top, h_bot = hs[i], hs[i + 1]
            v_left, v_right = vs[j], vs[j + 1]
            x0, x1 = v_left.x, v_right.x
            y0, y1 = h_top.y, h_bot.y
            if (x1 - x0) < cfg.wa_table_cell_min_side_pt:
                continue
            if (y1 - y0) < cfg.wa_table_cell_min_side_pt:
                continue
            # The candidate's perimeter must be sufficiently bordered.
            # Each side must be at least roughly aligned with the cluster that defines it.
            top_cov = max(0.0, min(h_top.x1, x1) - max(h_top.x0, x0)) / (x1 - x0)
            bot_cov = max(0.0, min(h_bot.x1, x1) - max(h_bot.x0, x0)) / (x1 - x0)
            left_cov = max(0.0, min(v_left.y1, y1) - max(v_left.y0, y0)) / (y1 - y0)
            right_cov = max(0.0, min(v_right.y1, y1) - max(v_right.y0, y0)) / (y1 - y0)
            avg_cov = (top_cov + bot_cov + left_cov + right_cov) / 4.0
            if avg_cov < cfg.wa_table_border_completeness_min:
                continue
            # Skip if any text span overlaps the cell interior (with small inset).
            inset = 2.0
            cell_text_rect = fitz.Rect(x0 + inset, y0 + inset, x1 - inset, y1 - inset)
            has_text = False
            for tx0, ty0, tx1, ty1, _t in text_lines:
                if fitz.Rect(tx0, ty0, tx1, ty1).intersects(cell_text_rect):
                    has_text = True
                    break
            if has_text:
                continue
            cells.append((BBox(x0, y0, x1, y1, page_no), h_top, h_bot, v_left, v_right))

    if not cells:
        return []

    # A real table has ≥ 2 cells.  A single 1 × 1 candidate is just a rectangle
    # (typically a diagram element, e.g. the central rect in a symmetry figure);
    # reject those here so the ``box`` pass handles them (with text-overlap check).
    if len(cells) < 2:
        return []

    # Reject cells whose interiors contain many vector paths — those are diagram
    # boxes (classification trees, frequency-tree branch labels, etc.), not empty
    # answer cells.
    if page is not None:
        filtered: list[tuple[BBox, _HRule, _HRule, _VRule, _VRule]] = []
        for c in cells:
            bb = c[0]
            interior = fitz.Rect(bb.x0 + 2, bb.y0 + 2, bb.x1 - 2, bb.y1 - 2)
            path_count = 0
            for d in page.get_drawings():
                dr = d.get("rect")
                if dr is None:
                    continue
                if not fitz.Rect(dr).intersects(interior):
                    continue
                # Skip the cell's own border drawings (full-size rects)
                if dr.width > (bb.x1 - bb.x0) * 0.9 and dr.height > (bb.y1 - bb.y0) * 0.9:
                    continue
                path_count += 1
                if path_count > 4:
                    break
            if path_count <= 4:
                filtered.append(c)
        cells = filtered
        if len(cells) < 2:
            return []

    out_bboxes = [c[0] for c in cells]

    # Mark consumed rules so downstream passes skip them.
    used_h = {id(c[1]) for c in cells} | {id(c[2]) for c in cells}
    used_v = {id(c[3]) for c in cells} | {id(c[4]) for c in cells}
    for r in h_rules:
        if id(r) in used_h:
            r.consumed = True
    for r in v_rules:
        if id(r) in used_v:
            r.consumed = True

    return out_bboxes


def _find_mark_indicator_near(
    text_lines: list[tuple[float, float, float, float, str]],
    y: float,
    x_end: float,
    cfg: ParserConfig,
) -> bool:
    """Return True if a ``[n]`` bracket appears on the same line or just below the rule at *y*.

    Used by the short_line pass to require evidence that the single rule is actually
    an answer slot (rather than a decorative underline).
    """
    import re

    pat = re.compile(r"\[\s*\d+\s*\]")
    for tx0, ty0, tx1, ty1, t in text_lines:
        if not pat.search(t):
            continue
        ty_center = (ty0 + ty1) * 0.5
        if abs(ty_center - y) <= cfg.wa_mark_indicator_proximity_pt:
            return True
        # Sometimes [n] sits right at the rule's right margin on the next line down.
        if 0 <= (ty_center - y) <= cfg.wa_mark_indicator_proximity_pt and tx1 >= x_end - 30:
            return True
    return False


def _classify_multi_line(
    h_rules: list[_HRule],
    cell_width: float,
    cfg: ParserConfig,
    page_no: int,
) -> list[BBox]:
    """Group ≥2 evenly-spaced unclaimed horizontal rules into multi-line writing areas.

    Bbox height encodes the writing space; the frontend derives `<textarea rows>` from
    bbox height ÷ typical line pitch.
    """
    free = sorted([r for r in h_rules if not r.consumed], key=lambda r: r.y)
    if len(free) < cfg.wa_lines_min_count:
        return []

    out: list[BBox] = []
    # Greedy: walk top-to-bottom, start a stack when we see two rules with similar
    # pitch and column-coverage; extend while the next rule keeps the same pitch.
    used: set[int] = set()
    for i in range(len(free)):
        if i in used:
            continue
        stack = [free[i]]
        # Try to extend.
        last = free[i]
        first_pitch: float | None = None
        for j in range(i + 1, len(free)):
            if j in used:
                continue
            r = free[j]
            pitch = r.y - last.y
            if pitch <= 0:
                continue
            # Lines should be roughly the same length (column-aligned).
            if abs(r.length - last.length) > max(40.0, last.length * 0.3):
                continue
            if first_pitch is None:
                # First candidate pitch — initialize.
                if pitch > 40.0:  # rules too far apart to be a writing-area stack
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

        # Column-coverage check.
        avg_len = sum(r.length for r in stack) / len(stack)
        if avg_len / max(cell_width, 1.0) < cfg.wa_lines_min_column_coverage_frac:
            continue

        x0 = min(r.x0 for r in stack)
        x1 = max(r.x1 for r in stack)
        y0 = stack[0].y
        y1 = stack[-1].y
        # Pad by half a line-pitch so the textarea covers the writable space, not
        # just the lines themselves.
        pitch = (y1 - y0) / max(len(stack) - 1, 1)
        y0_pad = y0 - pitch * 0.7
        y1_pad = y1 + pitch * 0.3
        out.append(BBox(x0, y0_pad, x1, y1_pad, page_no))

        for s in stack:
            s.consumed = True
        used.update(range(i, i + len(stack)))

    return out


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
    it (those are continuation lines of the same answer).  A new label starts a
    fresh slot.

    Returns ``[(bbox, kind), ...]`` where kind is ``"short_line"`` for a slot of
    one rule and ``"lines"`` for a slot of ≥ 2 rules.
    """
    import re

    LABEL_RE = re.compile(
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

    def label_of_line(t: str, dot_run_start_text: str | None = None) -> str | None:
        # Strip dot run + trailing [n] marker to recover the prefix
        stripped = re.sub(r"[.·•_\- ]{4,}.*$", "", t).strip()
        if not stripped:
            return None
        if len(stripped) > 40 or "?" in stripped:
            return None
        m = LABEL_RE.match(stripped)
        if not m:
            return None
        return m.group(0).strip()

    # Build per-rule label info.
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
                continue  # text doesn't precede the rule
            lbl = label_of_line(t)
            if lbl:
                break
        rule_label[id(r)] = lbl

    # Only proceed if ≥ 2 distinct labels appear in this leaf — otherwise the
    # multi-line classifier handles the stack correctly.
    distinct_labels = {v for v in rule_label.values() if v}
    if len(distinct_labels) < 2:
        return []

    out: list[tuple[BBox, str]] = []
    i = 0
    while i < len(free):
        if rule_label[id(free[i])] is None:
            i += 1
            continue
        # Start of a labeled slot.  Collect this rule + any unlabeled rules
        # vertically adjacent (within ~28pt below).
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

    Catches Cambridge stacked-answer patterns like ::

        t = ........
        w = ........  [4]

    The legacy detector requires ``[n]`` to follow the dots on the same line, so it
    only finds the ``w =`` slot; this pass picks up the ``t =`` slot.
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
            if tx1 > r.x0 + 4 or r.x0 - tx1 > 40:
                continue
            overlay_h = cfg.wa_equation_blank_max_height_pt
            # Place the box so the dots sit near the bottom and the typing area is above.
            bb = BBox(r.x0, r.y - (overlay_h - 4.0), r.x1, r.y + 4.0, page_no)
            out.append(bb)
            r.consumed = True
            break
    return out


def _classify_short_line(
    h_rules: list[_HRule],
    text_lines: list[tuple[float, float, float, float, str]],
    cfg: ParserConfig,
    page_no: int,
) -> list[BBox]:
    """Single rule near a ``[n]`` mark indicator OR a chain of short rules on one baseline.

    Two patterns produce ``short_line`` regions:

    - **Single**: one unclaimed horizontal rule ≥ ``wa_short_line_min_length_pt`` long with a
      ``[n]`` indicator nearby (same line or the next).
    - **Chain**: ≥ 2 short rules sharing a y baseline (within ``wa_chain_blank_baseline_tol_pt``),
      e.g. Cambridge ``..... < ..... < .....`` lines — each rule becomes its own slot if
      it meets ``wa_chain_blank_min_length_pt``.
    """
    out: list[BBox] = []

    # Group unclaimed rules by baseline.
    free = [r for r in h_rules if not r.consumed]
    groups: dict[int, list[_HRule]] = {}
    bucket = max(1, int(cfg.wa_chain_blank_baseline_tol_pt))
    for r in free:
        key = int(r.y / bucket)
        groups.setdefault(key, []).append(r)
        # also bucket key-1 / key+1 to be safe with rounding boundaries
    # Merge adjacent buckets within tolerance.
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
            for r in group:
                if r.length < cfg.wa_chain_blank_min_length_pt:
                    continue
                y0 = r.y - 12.0
                y1 = r.y + 4.0
                out.append(BBox(r.x0, y0, r.x1, y1, page_no))
                r.consumed = True
            continue
        r = group[0]
        if r.length < cfg.wa_short_line_min_length_pt:
            continue
        if not _find_mark_indicator_near(text_lines, r.y, r.x1, cfg):
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
    """Rules at the end of a long text sentence → narrative fill-in-blank.

    Cambridge "Fill in the gaps" patterns produce 3–5 lines like ::

        The planets nearest the Sun are small and ........
        The planets furthest from the Sun are large and ........
        ...

    Each rule is short (well under the column width) and preceded by ≥ 20 chars
    of prose.  The multi-line classifier rejects them (too short), labeled-lines
    skips them (no short prefix label), so they need their own pass.
    """
    import re
    out: list[BBox] = []
    for r in h_rules:
        if r.consumed:
            continue
        if r.length >= cell_width * 0.6:
            continue  # full-width rule, leave to multi-line / lines classifier
        for tx0, ty0, tx1, ty1, t in text_lines:
            ty_c = (ty0 + ty1) * 0.5
            if abs(ty_c - r.y) > 6:
                continue
            if tx0 >= r.x0 - 2:
                continue
            prefix = re.sub(r"[.·•_\- ]{4,}.*$", "", t).strip()
            if len(prefix) < 20:
                continue
            out.append(BBox(r.x0, r.y - 12.0, r.x1, r.y + 4.0, page_no))
            r.consumed = True
            break
    return out


def _classify_similar_length_cluster(
    h_rules: list[_HRule],
    cfg: ParserConfig,
    page_no: int,
) -> list[BBox]:
    """≥4 unclaimed short rules of similar length → each is an answer slot.

    Catches probability-tree, table-completion, and other diagram patterns where
    the answer dots are short (below ``wa_short_line_min_length_pt``) and have no
    adjacent ``[n]`` indicator.
    """
    free = [r for r in h_rules if not r.consumed]
    if len(free) < 4:
        return []
    # Find the largest cluster of rules with similar length (within ±25% of each
    # other).  Skips outliers (e.g. a long stem underline that shouldn't drag the
    # mean away from a tight group of short answer slots).
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

    if len(best_cluster) < 4:
        return []
    out: list[BBox] = []
    for r in best_cluster:
        if r.length < cfg.wa_chain_blank_min_length_pt:
            continue
        out.append(BBox(r.x0, r.y - 12.0, r.x1, r.y + 4.0, page_no))
        r.consumed = True
    return out


def _classify_box(
    rects: list[_ClosedRect],
    h_rules: list[_HRule],
    text_lines: list[tuple[float, float, float, float, str]],
    leaf_bbox: BBox,
    cell_width: float,
    cfg: ParserConfig,
    page_no: int,
    page: fitz.Page | None = None,
) -> list[BBox]:
    """Explicit closed rectangles, or a large empty band with no rules/text and a
    nearby ``[n]`` mark indicator (otherwise the trailing whitespace at the end of
    a page would be flagged as an answer box)."""
    import re
    out: list[BBox] = []
    inset = 2.0

    # Pre-compute per-rectangle counts of interior content so we can reject:
    #   - diagrams (×-mark arrays, dot grids, branching trees inside the rect)
    #   - figure rectangles around photos / image blocks
    rect_interior_paths: dict[int, int] = {}
    rect_overlaps_image: dict[int, bool] = {}
    image_rects: list[fitz.Rect] = []
    if page is not None:
        # Image blocks via dict (type == 1 are images)
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") == 1:
                bb = block.get("bbox")
                if bb:
                    image_rects.append(fitz.Rect(bb))
        for rc in rects:
            if rc.consumed:
                continue
            cell = fitz.Rect(rc.x0 + inset, rc.y0 + inset, rc.x1 - inset, rc.y1 - inset)
            count = 0
            for d in page.get_drawings():
                dr = d.get("rect")
                if dr is None:
                    continue
                if not fitz.Rect(dr).intersects(cell):
                    continue
                if (abs(dr.x0 - rc.x0) < 1 and abs(dr.y0 - rc.y0) < 1 and
                        abs(dr.x1 - rc.x1) < 1 and abs(dr.y1 - rc.y1) < 1):
                    continue
                if dr.width < (rc.x1 - rc.x0) * 0.9 or dr.height < (rc.y1 - rc.y0) * 0.9:
                    count += 1
                if count > 8:
                    break
            rect_interior_paths[id(rc)] = count
            # Image overlap: any image block whose center is inside rc
            overlaps_image = False
            for ir in image_rects:
                cx = (ir.x0 + ir.x1) * 0.5
                cy = (ir.y0 + ir.y1) * 0.5
                if rc.x0 <= cx <= rc.x1 and rc.y0 <= cy <= rc.y1:
                    overlaps_image = True
                    break
            rect_overlaps_image[id(rc)] = overlaps_image

    for rc in rects:
        if rc.consumed:
            continue
        h = rc.y1 - rc.y0
        w = rc.x1 - rc.x0
        if h < cfg.wa_box_min_height_pt:
            continue
        if w / max(cell_width, 1.0) < cfg.wa_box_min_column_coverage_frac:
            continue
        cell_text_rect = fitz.Rect(rc.x0 + inset, rc.y0 + inset, rc.x1 - inset, rc.y1 - inset)
        has_text = False
        for tx0, ty0, tx1, ty1, _t in text_lines:
            if fitz.Rect(tx0, ty0, tx1, ty1).intersects(cell_text_rect):
                has_text = True
                break
        if has_text:
            continue
        if rect_interior_paths.get(id(rc), 0) > 8:
            continue
        if rect_overlaps_image.get(id(rc), False):
            continue
        out.append(BBox(rc.x0, rc.y0, rc.x1, rc.y1, page_no))
        rc.consumed = True

    # Empty-band detection: a tall vertical span inside the leaf with no rules and
    # no text.  Require evidence that this band is actually an answer area: a
    # ``[n]`` mark indicator must appear within ~40pt below the band (otherwise the
    # trailing empty space at the end of a page would always look like an answer
    # box).
    band_x0 = leaf_bbox.x0
    band_x1 = leaf_bbox.x1
    band_top = leaf_bbox.y0
    bracket_re = re.compile(r"\[\s*\d+\s*\]")
    sorted_lines = sorted(text_lines, key=lambda t: t[1])
    last_y = band_top
    candidates: list[tuple[float, float]] = []
    for tx0, ty0, tx1, ty1, _t in sorted_lines:
        if ty0 - last_y > cfg.wa_box_min_height_pt:
            candidates.append((last_y, ty0))
        last_y = max(last_y, ty1)
    if leaf_bbox.y1 - last_y > cfg.wa_box_min_height_pt:
        candidates.append((last_y, leaf_bbox.y1))

    for y0, y1 in candidates:
        if any(not r.consumed and y0 <= r.y <= y1 for r in h_rules):
            continue
        h = y1 - y0
        if h < cfg.wa_box_min_height_pt:
            continue
        if (band_x1 - band_x0) / max(cell_width, 1.0) < cfg.wa_box_min_column_coverage_frac:
            continue
        has_bracket = False
        for tx0, ty0, tx1, ty1, t in text_lines:
            if y1 - 6 <= ty0 <= y1 + 50 and bracket_re.search(t):
                has_bracket = True
                break
        if not has_bracket:
            continue
        out.append(BBox(band_x0, y0, band_x1, y1, page_no))

    return out


def detect_writing_areas(
    doc: fitz.Document, cfg: ParserConfig, q: Question
) -> list[WritingArea]:
    """Detect all answer-region kinds for leaf *q* in source-PDF coordinates.

    MCQ leaves and non-leaf nodes (with subquestions) should be gated upstream by
    ``assign_answer_field_bboxes``; this function returns ``[]`` if called with one
    anyway (defensive — never raises on shape mismatches).
    """
    if q.question_type == "multiple_choice" or q.subquestions:
        return []
    pi = q.bbox.page - 1
    if pi < 0 or pi >= len(doc):
        return []

    page = doc[pi]
    cx = (q.bbox.x0 + q.bbox.x1) * 0.5
    cy = (q.bbox.y0 + q.bbox.y1) * 0.5
    cell = cell_for_point(page, cx, cy)
    h0, h1 = clip_horizontal_bounds(doc, pi, cfg, cell)
    clip = fitz.Rect(h0, q.bbox.y0, h1, q.bbox.y1)
    cell_width = h1 - h0

    h_rules, v_rules, rects = _extract_vector_segments(page, clip, cfg)
    h_rules.extend(_extract_text_dotted_rules(page, clip, cfg))
    h_rules.sort(key=lambda r: r.y)

    text_lines = _text_lines_in(page, clip)

    out: list[WritingArea] = []

    # Pass 1: table grid (must run first so it claims its rules before stacks).
    for bb in _classify_table_grid(h_rules, v_rules, rects, text_lines, cfg, q.bbox.page, page=page):
        out.append(WritingArea(bbox=bb, kind="table_cell"))

    # Pass 2: equation blanks (text-pattern defined; consume only the rule on the
    # eq-blank's *actual line* — not the wide pad-to-next-anchor range, since that
    # would eat rules on neighbouring lines that belong to other answer slots like
    # the t = / w = stacked pattern).
    for original_bb in infer_equation_blank_bboxes(doc, cfg, q):
        line_top = original_bb.y0 + (
            cfg.equation_blank_pad_above_pt - cfg.equation_blank_nudge_top_pt
        )
        # Find the matching detected rule on this line (if any) and use ITS x-extent
        # — it's more precise than the legacy char-interpolated bbox, which can over-
        # or under-shoot by a few points.
        matching_rule: _HRule | None = None
        for r in h_rules:
            if abs(r.y - line_top) <= 8.0:
                if matching_rule is None or r.length > matching_rule.length:
                    matching_rule = r
                if not r.consumed:
                    r.consumed = True
        if matching_rule is not None:
            x0, x1 = matching_rule.x0, matching_rule.x1
            rule_baseline = matching_rule.y
        else:
            x0, x1 = original_bb.x0, original_bb.x1
            rule_baseline = line_top + 12.0
        overlay_h = cfg.wa_equation_blank_max_height_pt
        bb = BBox(
            x0,
            rule_baseline - (overlay_h - 4.0),
            x1,
            rule_baseline + 4.0,
            original_bb.page,
        )
        out.append(WritingArea(bbox=bb, kind="equation_blank"))

    # Pass 2b: secondary equation_blank — rules preceded by ``=`` text on the same
    # baseline but without a ``[n]`` indicator (e.g. Cambridge stacked answers
    # ``t = ........`` / ``w = ........ [4]`` where the mark applies to the pair).
    for bb in _classify_secondary_equation_blank(h_rules, text_lines, cfg, q.bbox.page):
        out.append(WritingArea(bbox=bb, kind="equation_blank"))

    # Pass 2c: labeled-line splitting.  Cambridge often labels separate answer slots
    # with a short prefix ("Statement", "Explanation", "1", "2", "Reason", "Name").
    # Group such rules so each labeled rule + any unlabeled continuation rules
    # immediately below it become one region — labels create boundaries between
    # otherwise-mergeable multi-line stacks.
    for bb, kind in _classify_labeled_lines(h_rules, text_lines, cfg, q.bbox.page):
        out.append(WritingArea(bbox=bb, kind=kind))

    # Pass 3: multi-line writing areas (anything left over that wasn't claimed by the
    # labeled-line pass).
    for bb in _classify_multi_line(h_rules, cell_width, cfg, q.bbox.page):
        out.append(WritingArea(bbox=bb, kind="lines"))

    # Pass 4: short single line.
    for bb in _classify_short_line(h_rules, text_lines, cfg, q.bbox.page):
        out.append(WritingArea(bbox=bb, kind="short_line"))

    # Pass 4c: narrative fill-in-blank — short dotted runs at the END of sentences
    # ("The planets nearest the Sun are small and ........").  Each is a separate
    # inline answer slot; no `[n]` proximity required.
    for bb in _classify_inline_blank(h_rules, text_lines, cell_width, cfg, q.bbox.page):
        out.append(WritingArea(bbox=bb, kind="short_line"))

    # Pass 4b: probability-tree / similar-length-cluster pattern.  When a leaf has
    # ≥4 unclaimed short rules of similar length (within ±25%), treat each as a
    # short-answer slot.  Catches Cambridge probability trees, "Complete the
    # table" sketches, and other diagrams where the answer slots are short dots
    # without an adjacent `[n]` indicator.
    for bb in _classify_similar_length_cluster(h_rules, cfg, q.bbox.page):
        out.append(WritingArea(bbox=bb, kind="short_line"))

    # Pass 5: explicit boxes + empty bands.
    for bb in _classify_box(rects, h_rules, text_lines, q.bbox, cell_width, cfg, q.bbox.page, page=page):
        out.append(WritingArea(bbox=bb, kind="box"))

    # Sort by (page, y0, x0) so consumers see a stable order.
    out.sort(key=lambda wa: (wa.bbox.page, wa.bbox.y0, wa.bbox.x0))
    return out
