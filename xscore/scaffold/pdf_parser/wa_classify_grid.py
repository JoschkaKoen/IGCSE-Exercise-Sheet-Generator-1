"""Grid-based classifiers for the writing-area detector.

Contains :func:`_classify_table_grid` (empty cells in vector-bordered tables) and
:func:`_classify_box` (explicit outlined rectangles, or large empty bands with a
nearby ``[n]`` mark indicator).
"""

from __future__ import annotations

import re

import fitz

from xscore.shared.models import BBox
from xscore.scaffold.pdf_parser.config import ParserConfig
from xscore.scaffold.pdf_parser.wa_signals import _ClosedRect, _HRule, _VRule


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
    span overlaps it.  Then a sequence of filters rejects:

    - Single 1×1 candidates (not really tables).
    - Cells without a row-mate (scattered pseudo-cells from tree diagrams).
    - Uniform-cell grids (shading / coordinate grids — student colours squares).
    - Cells whose interiors contain many vector paths (diagram boxes).
    """
    if len(h_rules) < 2 or len(v_rules) < 2:
        return []

    # Filter h_rules to those that span the table's x-range (between the
    # leftmost and rightmost verticals).  Internal short rules — e.g. the
    # filler-glyph dotted line that marks an empty cell as an answer slot —
    # are NOT row boundaries; if they participate in the adjacent-pair
    # pairing they break otherwise-valid cell rows into two failed cells.
    if v_rules:
        x_lo = min(v.x for v in v_rules) - 2
        x_hi = max(v.x for v in v_rules) + 2
        spanning_h = [
            r for r in h_rules
            if r.x0 <= x_lo + 5 and r.x1 >= x_hi - 5
        ]
        hs = sorted(spanning_h, key=lambda r: r.y) if len(spanning_h) >= 2 else sorted(h_rules, key=lambda r: r.y)
    else:
        hs = sorted(h_rules, key=lambda r: r.y)

    cells: list[tuple[BBox, _HRule, _HRule, _VRule, _VRule]] = []
    all_candidates: list[tuple[BBox, _HRule, _HRule, _VRule, _VRule]] = []
    text_rejected_h_ids: set[int] = set()  # h_top ids of candidates rejected for text
    text_rejected_pairs: set[tuple[int, int]] = set()  # (h_top_id, h_bot_id)
    for i in range(len(hs) - 1):
        h_top, h_bot = hs[i], hs[i + 1]
        y0, y1 = h_top.y, h_bot.y
        if (y1 - y0) < cfg.wa_table_cell_min_side_pt:
            continue
        # Only verticals that actually span this row participate.  Without this
        # filter, verticals from a *different* table on the same page get mixed
        # in via the global sort and the adjacent-pair logic pairs them across
        # tables (e.g. Fig 3.1's left vertical paired with the sequence row's
        # internal divider, producing 0% coverage on the cell sides).
        spanning_v = sorted(
            (v for v in v_rules if v.y0 <= y0 + 2 and v.y1 >= y1 - 2),
            key=lambda v: v.x,
        )
        if len(spanning_v) < 2:
            continue
        for j in range(len(spanning_v) - 1):
            v_left, v_right = spanning_v[j], spanning_v[j + 1]
            x0, x1 = v_left.x, v_right.x
            if (x1 - x0) < cfg.wa_table_cell_min_side_pt:
                continue
            top_cov = max(0.0, min(h_top.x1, x1) - max(h_top.x0, x0)) / (x1 - x0)
            bot_cov = max(0.0, min(h_bot.x1, x1) - max(h_bot.x0, x0)) / (x1 - x0)
            left_cov = max(0.0, min(v_left.y1, y1) - max(v_left.y0, y0)) / (y1 - y0)
            right_cov = max(0.0, min(v_right.y1, y1) - max(v_right.y0, y0)) / (y1 - y0)
            avg_cov = (top_cov + bot_cov + left_cov + right_cov) / 4.0
            if avg_cov < cfg.wa_table_border_completeness_min:
                continue
            # Require each individual side to also have meaningful coverage —
            # a 0.81 average with one side at 0.26 typically indicates a giant
            # "page-frame" candidate (top h_rule plus a much-shorter inner
            # h_rule) rather than a real table row.  Without this guard the
            # candidate's rules get consumed and the inner short rules are
            # lost to downstream classifiers (math paper Q26 ``t = ……``).
            if min(top_cov, bot_cov, left_cov, right_cov) < 0.5:
                continue
            candidate = (BBox(x0, y0, x1, y1, page_no), h_top, h_bot, v_left, v_right)
            all_candidates.append(candidate)
            inset = 2.0
            cell_text_rect = fitz.Rect(x0 + inset, y0 + inset, x1 - inset, y1 - inset)
            has_text = False
            for tx0, ty0, tx1, ty1, _t in text_lines:
                if not fitz.Rect(tx0, ty0, tx1, ty1).intersects(cell_text_rect):
                    continue
                # Cambridge marks empty table cells with filler-glyph runs
                # (e.g. ``..............`` in biology paper 62 Q1ai's C cell).
                # Treat filler-only lines as "empty" so the cell is kept as an
                # answer slot; only real prose / numbers count as "text".
                stripped_alphanum = "".join(c for c in _t if c.isalnum())
                if not stripped_alphanum:
                    continue
                has_text = True
                break
            if has_text:
                text_rejected_h_ids.add(id(h_top))
                text_rejected_pairs.add((id(h_top), id(h_bot)))
                continue
            cells.append(candidate)

    # Even when no cells are kept (all contain text — e.g. a labelled
    # reference table like Fig 3.1 with A-E rows), consume the table's rules
    # so the multi-line / chain passes don't pick up its borders as a fake
    # answer area.
    def _consume_table_extent(table_cells: list[tuple[BBox, _HRule, _HRule, _VRule, _VRule]]) -> None:
        if not table_cells:
            return
        min_y = min(c[0].y0 for c in table_cells)
        max_y = max(c[0].y1 for c in table_cells)
        min_x = min(c[0].x0 for c in table_cells)
        max_x = max(c[0].x1 for c in table_cells)
        for r in h_rules:
            if min_y - 5 <= r.y <= max_y + 5:
                if not (r.x1 < min_x or r.x0 > max_x):
                    r.consumed = True
        for r in v_rules:
            if r.y1 >= min_y - 5 and r.y0 <= max_y + 5:
                if min_x - 5 <= r.x <= max_x + 5:
                    r.consumed = True

    # Always consume rules of *all* candidate cells (kept AND rejected) so
    # reference tables coexisting on the same leaf — e.g. Q3ai's Fig 3.1 (all
    # text) plus the sequence-row table (with empty cells) — both contribute
    # their rule geometry to the consumed set.  Without this, the
    # text-containing reference table's borders get picked up as fake
    # multi-line answer areas.
    if all_candidates:
        _consume_table_extent(all_candidates)

    if not cells:
        return []
    # Single kept cell is allowed only when it's part of a larger table — the
    # row pair must also be present among text-rejected cells (other columns
    # in the same row had prose).  Catches Cambridge tables where exactly one
    # value cell is empty, e.g. biology paper 62 Q1ai's Table 1.1 beaker C
    # row.  Without this exception we'd require ≥ 2 empty cells.
    if len(cells) < 2:
        only = cells[0]
        if (id(only[1]), id(only[2])) not in text_rejected_pairs:
            return []

    # Row-mate requirement: a kept cell must either share its (h_top, h_bot) pair
    # with another kept cell (real table with multiple empty cells per row), OR
    # share that rule pair with a text-rejected cell (the row has prose cells in
    # other columns).  Tree pseudo-cells share rules with nothing → drop.
    row_signature: dict[tuple[int, int], int] = {}
    for c in cells:
        _, h_top, h_bot, _, _ = c
        key = (id(h_top), id(h_bot))
        row_signature[key] = row_signature.get(key, 0) + 1
    cells = [
        c for c in cells
        if row_signature.get((id(c[1]), id(c[2])), 0) >= 2
        or (id(c[1]), id(c[2])) in text_rejected_pairs
    ]
    if not cells:
        return []
    if len(cells) < 2:
        only = cells[0]
        if (id(only[1]), id(only[2])) not in text_rejected_pairs:
            return []

    # Uniform-cell grid reject (shading grids).  Real Cambridge answer tables
    # often share rule pairs with text-containing cells (a header row, a label
    # column, a pre-filled example cell like the "B" in a sequence row); those
    # rule pairs land in ``text_rejected_h_ids``.  If a uniform-cell cluster
    # shares ANY rule pair with a text-rejected cell, it's an answer table.
    widths = [c[0].x1 - c[0].x0 for c in cells]
    heights = [c[0].y1 - c[0].y0 for c in cells]
    if widths and heights:
        w_ratio = max(widths) / max(min(widths), 1.0)
        h_ratio = max(heights) / max(min(heights), 1.0)
        avg_side = (sum(widths) + sum(heights)) / (2 * len(cells))
        if w_ratio < 1.25 and h_ratio < 1.25 and avg_side < 60:
            shares_text_row = any(
                id(c[1]) in text_rejected_h_ids or id(c[2]) in text_rejected_h_ids
                for c in cells
            )
            if not shares_text_row:
                return []

    # Interior-path filter.
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
                if dr.x1 < interior.x0 or dr.x0 > interior.x1 or dr.y1 < interior.y0 or dr.y0 > interior.y1:
                    continue
                if dr.width > (bb.x1 - bb.x0) * 0.9 and dr.height > (bb.y1 - bb.y0) * 0.9:
                    continue
                path_count += 1
                if path_count > 4:
                    break
            if path_count <= 4:
                filtered.append(c)
        cells = filtered
        if not cells:
            return []
        if len(cells) < 2:
            only = cells[0]
            if (id(only[1]), id(only[2])) not in text_rejected_pairs:
                return []

    out_bboxes = [c[0] for c in cells]

    # Consume the full table's rule geometry — including rules bordering the
    # header / pre-filled rows that were rejected because they contain text.
    # Without this, the header row's borders get picked up by the multi-line /
    # chain passes as fake answer areas.  Use *all candidate cells* (including
    # text-containing ones) to compute the table's extent.
    related: list[tuple[BBox, _HRule, _HRule, _VRule, _VRule]] = list(cells)
    kept_h_ids = {id(c[1]) for c in cells} | {id(c[2]) for c in cells}
    kept_v_ids = {id(c[3]) for c in cells} | {id(c[4]) for c in cells}
    for cand in all_candidates:
        # Only include candidates that share at least one rule with a kept cell
        # — those are genuinely the SAME table.  Unrelated tables / pseudo-cells
        # elsewhere on the page stay independent.
        if (id(cand[1]) in kept_h_ids or id(cand[2]) in kept_h_ids
                or id(cand[3]) in kept_v_ids or id(cand[4]) in kept_v_ids):
            related.append(cand)
    min_y = min(c[0].y0 for c in related)
    max_y = max(c[0].y1 for c in related)
    min_x = min(c[0].x0 for c in related)
    max_x = max(c[0].x1 for c in related)
    for r in h_rules:
        if min_y - 5 <= r.y <= max_y + 5:
            if not (r.x1 < min_x or r.x0 > max_x):
                r.consumed = True
    for r in v_rules:
        if r.y1 >= min_y - 5 and r.y0 <= max_y + 5:
            if min_x - 5 <= r.x <= max_x + 5:
                r.consumed = True

    return out_bboxes


_BRACKET_RE = re.compile(r"\[\s*\d+\s*\]")


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
    nearby ``[n]`` mark indicator (otherwise trailing whitespace at the end of a
    page would always look like an answer box).

    Also marks the borders of *rejected* figure rectangles as consumed so the
    chain / multi-line / cluster passes don't pick them up as short_line slots.
    """
    out: list[BBox] = []
    inset = 2.0

    rect_interior_paths: dict[int, int] = {}
    rect_overlaps_image: dict[int, bool] = {}
    image_rects: list[fitz.Rect] = []
    if page is not None:
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
                if dr.x1 < cell.x0 or dr.x0 > cell.x1 or dr.y1 < cell.y0 or dr.y0 > cell.y1:
                    continue
                if (abs(dr.x0 - rc.x0) < 1 and abs(dr.y0 - rc.y0) < 1 and
                        abs(dr.x1 - rc.x1) < 1 and abs(dr.y1 - rc.y1) < 1):
                    continue
                if dr.width < (rc.x1 - rc.x0) * 0.9 or dr.height < (rc.y1 - rc.y0) * 0.9:
                    count += 1
                if count > 8:
                    break
            rect_interior_paths[id(rc)] = count
            overlaps_image = False
            for ir in image_rects:
                cx = (ir.x0 + ir.x1) * 0.5
                cy = (ir.y0 + ir.y1) * 0.5
                if rc.x0 <= cx <= rc.x1 and rc.y0 <= cy <= rc.y1:
                    overlaps_image = True
                    break
            rect_overlaps_image[id(rc)] = overlaps_image

    def _consume_border_rules(rect_xy: tuple[float, float, float, float]) -> None:
        rx0, ry0, rx1, ry1 = rect_xy
        for r in h_rules:
            if r.consumed:
                continue
            for yedge in (ry0, ry1):
                if abs(r.y - yedge) <= 3.0:
                    overlap = max(0.0, min(r.x1, rx1) - max(r.x0, rx0))
                    if overlap >= (rx1 - rx0) * 0.7:
                        r.consumed = True
                        break

    for rc in rects:
        if rc.consumed:
            continue
        h = rc.y1 - rc.y0
        w = rc.x1 - rc.x0
        is_figure = (
            rect_interior_paths.get(id(rc), 0) > 8
            or rect_overlaps_image.get(id(rc), False)
        )
        cell_text_rect = fitz.Rect(rc.x0 + inset, rc.y0 + inset, rc.x1 - inset, rc.y1 - inset)
        has_text = False
        for tx0, ty0, tx1, ty1, _t in text_lines:
            if fitz.Rect(tx0, ty0, tx1, ty1).intersects(cell_text_rect):
                has_text = True
                break
        if is_figure or has_text:
            _consume_border_rules((rc.x0, rc.y0, rc.x1, rc.y1))
            continue
        if h < cfg.wa_box_min_height_pt:
            continue
        if w / max(cell_width, 1.0) < cfg.wa_box_min_column_coverage_frac:
            continue
        out.append(BBox(rc.x0, rc.y0, rc.x1, rc.y1, page_no))
        rc.consumed = True

    return out
