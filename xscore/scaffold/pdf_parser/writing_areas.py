"""Unified per-leaf detection of answer-region geometry.

Five kinds, classified in order so each pass claims its rule segments:

- ``table_cell``     — empty cell inside a vector-bordered table grid.
- ``equation_blank`` — Cambridge ``label = …… [n]`` slot, defined by a text pattern.
- ``lines``          — stack of ≥2 evenly-spaced horizontal rules (multi-line writing area).
- ``short_line``     — single horizontal rule near a ``[n]`` mark indicator, or chain
                       of short rules, or narrative inline blank.
- ``box``            — explicit outlined rectangle, or large empty band with a
                       nearby ``[n]`` indicator.

Implementation is split across three modules: :mod:`wa_signals` (rule / rect / text
extraction primitives), :mod:`wa_classify_grid` (table_cell + box), and
:mod:`wa_classify_rules` (line-based classifiers).  This file just orchestrates
the passes and applies a diagram-shape pre-filter that consumes h_rules inside
Bézier-bounded shapes (rounded rectangles, organ outlines) so figure borders
don't get re-detected as short_line stacks.
"""

from __future__ import annotations

import fitz

from xscore.shared.models import BBox, Question, WritingArea
from xscore.scaffold.pdf_parser.answer_fields import infer_equation_blank_bboxes
from xscore.scaffold.pdf_parser.config import ParserConfig
from xscore.scaffold.pdf_parser.layout import cell_for_point
from xscore.scaffold.pdf_parser.regions import clip_horizontal_bounds
from xscore.scaffold.pdf_parser.wa_geometry import bbox_for_equation_blank
from xscore.scaffold.pdf_parser.wa_signals import (
    _HRule,
    _extract_text_dotted_rules,
    _extract_vector_segments,
    _text_lines_in,
)
from xscore.scaffold.pdf_parser.wa_classify_grid import (
    _classify_box,
    _classify_table_grid,
)
from xscore.scaffold.pdf_parser.wa_classify_rules import (
    _classify_inline_blank,
    _classify_labeled_lines,
    _classify_multi_line,
    _classify_secondary_equation_blank,
    _classify_short_line,
    _classify_similar_length_cluster,
)


# Pre-pass consumers.  All values lifted from inline literals during the
# Phase 1 refactor; calibration history lives in git.
_DIAGRAM_Y_PAD_PT = 3.0
_DIAGRAM_X_OVERLAP_FRAC = 0.5
_GRAPH_GRID_MIN_CROSSINGS = 5
_GRAPH_GRID_Y_PAD_PT = 2.0
_GRAPH_GRID_X_PAD_PT = 2.0

# _emit_equation_blanks: when no matching h_rule is found for a legacy eq_blank
# bbox, the synthetic rule baseline sits this far below ``line_top``.
_EQ_BLANK_SYNTHETIC_BASELINE_OFFSET_PT = 12.0

# _emit_equation_blanks: ``line_top`` is the TOP of the text line, but the
# detected h_rule sits at the BASELINE — typically ~6pt below for Cambridge
# body text.  Shift the match anchor by this offset so the matching window
# (``± wa_eq_blank_baseline_tol_pt``) actually covers the baseline.  Without
# this, lines with slightly taller fonts (physics paper 6X ~9pt offset) miss
# the match and the secondary equation-blank classifier re-emits a duplicate
# region against the same rule.
_EQ_BLANK_BASELINE_OFFSET_FROM_TOP_PT = 6.0


_DIAGRAM_MIN_DIAGONAL_SEGMENTS = 2
_DIAGRAM_DIAGONAL_MIN_DX_PT = 5.0
_DIAGRAM_DIAGONAL_MIN_DY_PT = 5.0

# Hatched / stippled fill: ≥ _STIPPLE_MIN_RULES parallel h_rules within a
# vertical band of _STIPPLE_BAND_HEIGHT_PT pt with IDENTICAL x-ranges (within
# _STIPPLE_X_TOL_PT pt at each edge).  Cambridge sometimes draws answer boxes
# as a hatched fill (a_level_biology specimen Q1aii — ~70 horizontal stripes
# spanning a 4-column block), producing dozens of false short_line / cluster
# regions if left alone.  Real Cambridge answer-line stacks have ≥ 12pt
# inter-line spacing AND start/end at different x-positions per line; stripes
# are rigid copies, so the strict x-tolerance distinguishes them safely.
_STIPPLE_MIN_RULES = 4
_STIPPLE_BAND_HEIGHT_PT = 12.0
# Cambridge sometimes interleaves stripes at alternating x-offsets ~2pt apart
# (a_level_biology specimen Q1aii draws even/odd rows offset by 2pt), so the
# tolerance must absorb that drift.  Real answer-line stacks vary their x
# extent by far more (the "[n]" mark indicator shifts the right edge by
# 30–50pt), so 3pt stays well below the discrimination threshold.
_STIPPLE_X_TOL_PT = 3.0


def _consume_rules_inside_diagrams(
    page: fitz.Page, clip: fitz.Rect, h_rules: list[_HRule]
) -> None:
    """Consume h_rules that fall inside diagram shapes.

    Two kinds of drawings count as "figures":

    1. **Bézier-curved drawings.** Bacterial-reproduction rounded rectangles
       (biology paper 42 page 14), organ outlines in physiology diagrams,
       speech-bubble outlines, etc.
    2. **Drawings with ≥2 diagonal line segments.** Pyramids, prisms, cones,
       and other 3D shapes drawn with straight perspective edges (mathematics
       paper 32 Q2 pyramid).  Tables and answer-box rectangles are pure
       axis-aligned and never trip this threshold.

    Cambridge answer-area dotted lines are never enclosed in such shapes,
    so consuming rules here is safe.
    """
    for d in page.get_drawings():
        dr = d.get("rect")
        if dr is None:
            continue
        if dr.x1 < clip.x0 or dr.x0 > clip.x1 or dr.y1 < clip.y0 or dr.y0 > clip.y1:
            continue
        items = d.get("items") or []
        has_curves = any(it and it[0] == "c" for it in items)
        n_diagonals = 0
        for it in items:
            if not it or it[0] != "l" or len(it) < 3:
                continue
            p0, p1 = it[1], it[2]
            dx = abs(float(p0.x) - float(p1.x))
            dy = abs(float(p0.y) - float(p1.y))
            if dx > _DIAGRAM_DIAGONAL_MIN_DX_PT and dy > _DIAGRAM_DIAGONAL_MIN_DY_PT:
                n_diagonals += 1
                if n_diagonals >= _DIAGRAM_MIN_DIAGONAL_SEGMENTS:
                    break
        is_figure = has_curves or n_diagonals >= _DIAGRAM_MIN_DIAGONAL_SEGMENTS
        if not is_figure:
            continue
        dw = max(dr.x1 - dr.x0, 1.0)
        for hr in h_rules:
            if hr.consumed:
                continue
            if dr.y0 - _DIAGRAM_Y_PAD_PT <= hr.y <= dr.y1 + _DIAGRAM_Y_PAD_PT:
                x_overlap = max(0.0, min(hr.x1, dr.x1) - max(hr.x0, dr.x0))
                if x_overlap >= dw * _DIAGRAM_X_OVERLAP_FRAC:
                    hr.consumed = True


def _consume_rules_in_stippled_fills(h_rules: list[_HRule]) -> None:
    """Consume runs of parallel h_rules that form a hatched / stippled fill.

    Cambridge occasionally renders an answer area as a hatched rectangle —
    many parallel horizontal stripes every 2–4pt within a small vertical
    band (a_level_biology specimen paper 3 Q1aii uses this for a 4-column
    answer grid).  Each stripe becomes its own ``_HRule`` and downstream
    classifiers pick the bottom few up as overlapping ``short_line`` /
    ``similar_length_cluster`` regions.

    Stripes are rigid copies of one rule (identical x-range, even spacing)
    whereas a real Cambridge answer-line stack uses varying x-extents and
    ≥ 12pt vertical spacing, so the strict x-tolerance + small band height
    isolate stripes safely.
    """
    if len(h_rules) < _STIPPLE_MIN_RULES:
        return
    rules_sorted = sorted(h_rules, key=lambda r: r.y)
    n = len(rules_sorted)
    for i, anchor in enumerate(rules_sorted):
        if anchor.consumed:
            continue
        cluster = [anchor]
        for j in range(i + 1, n):
            r = rules_sorted[j]
            if r.y - anchor.y > _STIPPLE_BAND_HEIGHT_PT:
                break
            if (abs(r.x0 - anchor.x0) <= _STIPPLE_X_TOL_PT
                    and abs(r.x1 - anchor.x1) <= _STIPPLE_X_TOL_PT):
                cluster.append(r)
        if len(cluster) >= _STIPPLE_MIN_RULES:
            for r in cluster:
                r.consumed = True


def _consume_rules_in_graph_grid(
    h_rules: list[_HRule], v_rules: list[_VRule]
) -> None:
    """Consume h_rules inside a dense vertical-gridline cluster (graph paper).

    A horizontal rule with ≥5 crossing verticals is a graph gridline, not an
    answer line.  This catches plot grids (biology pulse-rate plot, physics
    distance-time plot, chemistry titration plot) where the y-axis number labels
    would otherwise trigger ``_classify_labeled_lines`` and produce false yellow
    stripes across the graph.
    """
    if len(v_rules) < _GRAPH_GRID_MIN_CROSSINGS:
        return
    for hr in h_rules:
        if hr.consumed:
            continue
        crossing = sum(
            1 for v in v_rules
            if (
                v.y0 - _GRAPH_GRID_Y_PAD_PT <= hr.y <= v.y1 + _GRAPH_GRID_Y_PAD_PT
                and hr.x0 - _GRAPH_GRID_X_PAD_PT <= v.x <= hr.x1 + _GRAPH_GRID_X_PAD_PT
            )
        )
        if crossing >= _GRAPH_GRID_MIN_CROSSINGS:
            hr.consumed = True


def _emit_equation_blanks(
    doc: fitz.Document,
    cfg: ParserConfig,
    q: Question,
    h_rules: list[_HRule],
) -> list[tuple[BBox, str]]:
    """Convert :func:`infer_equation_blank_bboxes` results to overlay bboxes.

    For each legacy eq-blank bbox, find the detected rule on the same baseline
    and use its precise x-extent (the legacy char-interpolation can be off by
    several points).  Emits ``equation_blank`` regions and marks consumed rules.
    Return shape matches the ``_classify_*`` classifiers so the orchestrator's
    loop is uniform.
    """
    out: list[tuple[BBox, str]] = []
    for original_bb in infer_equation_blank_bboxes(doc, cfg, q):
        line_top = original_bb.y0 + (
            cfg.equation_blank_pad_above_pt - cfg.equation_blank_nudge_top_pt
        )
        line_baseline = line_top + _EQ_BLANK_BASELINE_OFFSET_FROM_TOP_PT
        matching_rule: _HRule | None = None
        for r in h_rules:
            if abs(r.y - line_baseline) <= cfg.wa_eq_blank_baseline_tol_pt:
                if matching_rule is None or r.length > matching_rule.length:
                    matching_rule = r
                if not r.consumed:
                    r.consumed = True
        if matching_rule is not None:
            rule_for_bbox = matching_rule
        else:
            # No matching detected rule on this baseline — synthesize one from
            # the legacy text-pattern bbox so we can share bbox_for_equation_blank.
            rule_for_bbox = _HRule(
                y=line_top + _EQ_BLANK_SYNTHETIC_BASELINE_OFFSET_PT,
                x0=original_bb.x0,
                x1=original_bb.x1,
                dotted=False,
            )
        bb = bbox_for_equation_blank(rule_for_bbox, original_bb.page, cfg)
        out.append((bb, "equation_blank"))
    return out


def _detect_in_region(
    doc: fitz.Document,
    cfg: ParserConfig,
    q: Question,
    region: BBox,
    run_eq_blank_pattern: bool,
) -> list[WritingArea]:
    """Run all classification passes against a single page-bbox region."""
    pi = region.page - 1
    if pi < 0 or pi >= len(doc):
        return []
    page = doc[pi]
    cx = (region.x0 + region.x1) * 0.5
    cy = (region.y0 + region.y1) * 0.5
    cell = cell_for_point(page, cx, cy)
    h0, h1 = clip_horizontal_bounds(doc, pi, cfg, cell)
    clip = fitz.Rect(h0, region.y0, h1, region.y1)
    cell_width = h1 - h0

    h_rules, v_rules, rects = _extract_vector_segments(page, clip, cfg)
    h_rules.extend(_extract_text_dotted_rules(page, clip, cfg))
    h_rules.sort(key=lambda r: r.y)

    text_lines = _text_lines_in(page, clip)
    _consume_rules_inside_diagrams(page, clip, h_rules)
    _consume_rules_in_stippled_fills(h_rules)
    _consume_rules_in_graph_grid(h_rules, v_rules)

    # Classifier sequence — DO NOT REORDER.  Each pass mutates ``h_rules``'s
    # consumed flags so later passes don't re-detect claimed rules.  Critical
    # constraints encoded by this ordering:
    #  - ``table_grid`` first: its rule-consume strips reference-table borders
    #    before downstream classifiers see them (biology Q3ai).
    #  - ``similar_length_cluster`` before ``short_line``: chemistry Q3b's
    #    four unit-suffix answer lines need the cluster pass to fire before
    #    ``short_line`` claims the rule with the `[4]` indicator.
    #  - Legacy text-pattern ``_emit_equation_blanks`` only runs on the primary
    #    region; ``infer_equation_blank_bboxes`` operates on ``q.bbox`` and
    #    re-running it per continuation page is redundant.
    classifier_results: list[tuple[BBox, str]] = []
    classifier_results.extend(
        _classify_table_grid(h_rules, v_rules, rects, text_lines, cfg, region.page, page=page)
    )
    if run_eq_blank_pattern:
        classifier_results.extend(_emit_equation_blanks(doc, cfg, q, h_rules))
    classifier_results.extend(
        _classify_secondary_equation_blank(h_rules, text_lines, cfg, region.page)
    )
    classifier_results.extend(_classify_labeled_lines(h_rules, text_lines, cfg, region.page))
    classifier_results.extend(
        _classify_box(rects, h_rules, text_lines, region, cell_width, cfg, region.page, page=page)
    )
    classifier_results.extend(_classify_multi_line(h_rules, v_rules, cell_width, cfg, region.page))
    classifier_results.extend(
        _classify_similar_length_cluster(h_rules, v_rules, cfg, region.page)
    )
    classifier_results.extend(
        _classify_short_line(h_rules, v_rules, text_lines, cfg, region.page)
    )
    classifier_results.extend(
        _classify_inline_blank(h_rules, text_lines, cell_width, cfg, region.page)
    )

    return [WritingArea(bbox=bb, kind=kind) for bb, kind in classifier_results]


def detect_writing_areas(
    doc: fitz.Document, cfg: ParserConfig, q: Question
) -> list[WritingArea]:
    """Detect all answer-region kinds for leaf *q* in source-PDF coordinates.

    MCQ leaves and non-leaf nodes (with subquestions) should be gated upstream by
    ``assign_answer_field_bboxes``; this function returns ``[]`` if called with
    one anyway (defensive — never raises on shape mismatches).

    When the leaf has ``continuation_bboxes`` (its content spans page breaks),
    runs the full classification pipeline on each region and merges results.
    """
    if q.question_type == "multiple_choice" or q.subquestions:
        return []

    out: list[WritingArea] = []
    out.extend(_detect_in_region(doc, cfg, q, q.bbox, run_eq_blank_pattern=True))
    for cont_bb in q.continuation_bboxes:
        out.extend(_detect_in_region(doc, cfg, q, cont_bb, run_eq_blank_pattern=False))

    out.sort(key=lambda wa: (wa.bbox.page, wa.bbox.y0, wa.bbox.x0))
    return out
