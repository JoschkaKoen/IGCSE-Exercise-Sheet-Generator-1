"""Signal extraction for the writing-area detector.

Pulls horizontal / vertical rule segments, closed rectangles, text-rendered dotted
"rules" (filler-glyph runs on a baseline), and the per-leaf list of text lines out
of a PyMuPDF page.  No classification here — see :mod:`wa_classify_grid` and
:mod:`wa_classify_rules` for that.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

import fitz

from xscore.scaffold.pdf_parser.config import ParserConfig


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
    for it in items:
        if it[0] == "re" and len(it) >= 2:
            r = it[1]
            return (float(r.x0), float(r.y0), float(r.x1), float(r.y1))
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
        if r is not None:
            # fitz.Rect.intersects() returns False for zero-area rects, which is
            # exactly what perfectly horizontal/vertical line drawings produce.
            # Filter by manual bounds overlap instead.
            if r.x1 < clip.x0 or r.x0 > clip.x1 or r.y1 < clip.y0 or r.y0 > clip.y1:
                continue
        rect_xy = _is_rect_drawing(d)
        if rect_xy is not None:
            x0, y0, x1, y1 = rect_xy
            w, h = x1 - x0, y1 - y0
            if w >= cfg.wa_table_cell_min_side_pt and h >= cfg.wa_table_cell_min_side_pt:
                rects.append(_ClosedRect(x0=x0, y0=y0, x1=x1, y1=y1))
        items = d.get("items") or []
        for it in items:
            for sx0, sy0, sx1, sy1 in _segments_from_drawing(it):
                dx = abs(sx1 - sx0)
                dy = abs(sy1 - sy0)
                # Collect ALL horizontal and vertical segments (down to a 4pt
                # floor) so tables drawn with one-segment-per-cell, and
                # dashed graph gridlines drawn as many short solid segments,
                # get re-assembled by the cluster passes into logical rules.
                # The length filter is applied after clustering.
                if dy <= cfg.wa_h_rule_max_height_pt and dx >= 4.0:
                    x0, x1 = min(sx0, sx1), max(sx0, sx1)
                    y = (sy0 + sy1) * 0.5
                    h_segs.append((y, x0, x1, 0.0))
                elif dx <= cfg.wa_v_rule_max_width_pt and dy >= 4.0:
                    y0, y1 = min(sy0, sy1), max(sy0, sy1)
                    x = (sx0 + sx1) * 0.5
                    v_segs.append((x, y0, y1, 0.0))

    def _h_in_clip(s: tuple[float, float, float, float]) -> bool:
        y, x0, x1, _ = s
        return clip.y0 - 1 <= y <= clip.y1 + 1 and not (x1 < clip.x0 or x0 > clip.x1)

    def _v_in_clip(s: tuple[float, float, float, float]) -> bool:
        x, y0, y1, _ = s
        return clip.x0 - 1 <= x <= clip.x1 + 1 and not (y1 < clip.y0 or y0 > clip.y1)

    h_segs = [s for s in h_segs if _h_in_clip(s)]
    v_segs = [s for s in v_segs if _v_in_clip(s)]

    h_rules = _cluster_horizontals(h_segs, cfg.wa_rule_cluster_y_tol_pt)
    h_rules = [r for r in h_rules if r.length >= cfg.wa_h_rule_min_length_pt]
    v_rules = _cluster_verticals(v_segs, cfg.wa_rule_cluster_x_tol_pt)
    v_rules = [r for r in v_rules if r.length >= cfg.wa_v_rule_min_length_pt]

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

    Cambridge renders answer-line dots in three patterns we have to handle:

    1. A long run of the same character — e.g. ``.................`` or
       ``ĭĭĭĭĭĭĭĭĭĭĭ`` (some embedded fonts map the dot glyph to an exotic codepoint
       like U+012D — see Q21 on the 0580 March 2025 paper).
    2. Spaced-out dots like ``. . . . . . . . . .``, where the dots and the spaces
       between them alternate, often as one-character-per-span runs.
    3. Mixed filler glyphs — e.g. ``..…………..`` (2 periods + 6 horizontal-ellipsis
       U+2026 chars + 2 periods) on the biology 0610 paper 32 genetics diagram.
       Each filler char counts toward the run regardless of which exact codepoint it is.

    Uses rawdict so each character has its own glyph bbox — span-level
    interpolation places dot indices many points off when the span mixes wide
    letters with narrow dot glyphs.
    """
    rules: list[_HRule] = []

    def is_filler(c: str) -> bool:
        if c.isspace():
            return False
        if c.isascii() and c.isalnum():
            return False
        if c in "()[]{}<>,;:!?\"'`":
            return False
        # Use Unicode category to distinguish dot-like punctuation from math
        # operators.  ``.`` and ``…`` are both ``Po`` (other punctuation); ``×``
        # and ``+`` are ``Sm`` (math symbol) and must NOT be filler — they sit
        # *between* genuinely separate answer fields and would otherwise glue
        # them into one giant run.  ``ĭ``-style font tricks (Letter category
        # used visually as a dot) are allowed only outside ASCII.
        cat = unicodedata.category(c)
        if cat in ("Po", "Pd", "Pc"):
            return True
        if cat.startswith("L") and ord(c) >= 128:
            return True
        return False

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

            i = 0
            while i < len(line_text):
                c = line_text[i]
                if not is_filler(c):
                    i += 1
                    continue
                j = i + 1
                count = 1
                last_filler_x1 = char_x1[i] if i < len(char_x1) else 0.0
                # Median filler glyph width — used to detect large gaps that mean
                # "this is a new dotted segment", not "still the same one".
                filler_w = max((char_x1[i] - char_x0[i]) if i < len(char_x0) else 1.5, 1.5)
                while j < len(line_text):
                    nxt = line_text[j]
                    if is_filler(nxt):
                        # If the x-gap from the previous filler glyph is more than
                        # ~5x the typical filler glyph width, this is a new
                        # run, not a continuation.  Catches Cambridge genetics
                        # diagrams where "..………….. ..………….." renders as two
                        # separate visual fields on one text line.
                        if j < len(char_x0):
                            gap = char_x0[j] - last_filler_x1
                            if gap > filler_w * 5.0:
                                break
                            last_filler_x1 = char_x1[j]
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


_MARK_BRACKET_RE = re.compile(r"\[\s*\d+\s*\]")


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
    for tx0, ty0, tx1, ty1, t in text_lines:
        if not _MARK_BRACKET_RE.search(t):
            continue
        ty_center = (ty0 + ty1) * 0.5
        if abs(ty_center - y) <= cfg.wa_mark_indicator_proximity_pt:
            return True
        if 0 <= (ty_center - y) <= cfg.wa_mark_indicator_proximity_pt and tx1 >= x_end - 30:
            return True
    return False
