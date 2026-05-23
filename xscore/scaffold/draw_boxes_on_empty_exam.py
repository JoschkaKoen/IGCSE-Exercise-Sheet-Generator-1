"""Draw color-coded scaffold bounding boxes on a copy of the vector exam PDF.

Each question's ``bbox`` and figure ``images`` get a distinct stroke color derived
from golden-ratio–spaced HSV hues (S=0.82, V=0.92) so overlapping exercises are easy
to tell apart at a glance.

``writing_areas`` are drawn on top of the exercise outlines in kind-specific colours
so detected answer slots are visually distinct:

- ``equation_blank`` — teal
- ``short_line``    — yellow
- ``lines``         — cyan
- ``box``           — magenta
- ``table_cell``    — orange

``equation_blank_bboxes`` is the legacy field; the canonical source is now
``writing_areas`` (see :mod:`xscore.scaffold.pdf_parser.writing_areas`).  We still
honour ``equation_blank_bboxes`` for back-compat when ``writing_areas`` is empty.
"""

from __future__ import annotations

import colorsys
from pathlib import Path

import fitz

from xscore.shared.models import BBox, Question, flatten_questions

# Golden-ratio increment for hue stepping (φ⁻¹ ≈ 0.6180339887).
_PHI_INV = 0.6180339887
_HUE_S = 0.82
_HUE_V = 0.92

# Per-kind colours for writing_areas.
_KIND_COLOURS: dict[str, tuple[float, float, float]] = {
    "equation_blank": (0.00, 0.52, 0.55),  # teal
    "short_line":     (1.00, 0.90, 0.00),  # yellow
    "lines":          (0.00, 0.80, 0.85),  # cyan
    "box":            (0.85, 0.20, 0.75),  # magenta
    "table_cell":     (1.00, 0.55, 0.10),  # orange
}
_DEFAULT_KIND_COLOUR = (0.5, 0.5, 0.5)


def _hsv_color(index: int) -> tuple[float, float, float]:
    h = (index * _PHI_INV) % 1.0
    return colorsys.hsv_to_rgb(h, _HUE_S, _HUE_V)


def _kind_colour(kind: str) -> tuple[float, float, float]:
    return _KIND_COLOURS.get(kind, _DEFAULT_KIND_COLOUR)


def _rects_for_question_node(
    q: Question, color_index: int
) -> tuple[
    list[tuple[int, fitz.Rect, tuple[float, float, float]]],
    list[tuple[int, fitz.Rect, tuple[float, float, float]]],
]:
    """Return ``(exercise_rects, writing_area_rects)`` for this node only.

    Exercise rects (the leaf's ``bbox`` plus figure images) use a per-question colour;
    writing-area rects use the kind-specific palette.
    """
    exercise_rects: list[tuple[int, fitz.Rect, tuple[float, float, float]]] = []
    writing_area_rects: list[tuple[int, fitz.Rect, tuple[float, float, float]]] = []
    color = _hsv_color(color_index)

    def _push_exercise(bb: BBox | None) -> None:
        if bb is None or bb.x1 <= bb.x0 or bb.y1 <= bb.y0:
            return
        exercise_rects.append((bb.page, fitz.Rect(bb.x0, bb.y0, bb.x1, bb.y1), color))

    def _push_writing(bb: BBox, kind: str) -> None:
        if bb.x1 <= bb.x0 or bb.y1 <= bb.y0:
            return
        writing_area_rects.append(
            (bb.page, fitz.Rect(bb.x0, bb.y0, bb.x1, bb.y1), _kind_colour(kind))
        )

    _push_exercise(q.bbox)
    for im in q.images:
        _push_exercise(im.bbox)

    if q.writing_areas:
        for wa in q.writing_areas:
            _push_writing(wa.bbox, wa.kind)
    else:
        # Back-compat: when writing_areas is empty, still draw equation_blank_bboxes
        # so older callers / cached scaffolds keep producing the same overlay output.
        for eb in q.equation_blank_bboxes:
            _push_writing(eb, "equation_blank")

    return exercise_rects, writing_area_rects


def write_scaffold_boxes_pdf(
    exam_pdf: Path,
    questions: list[Question],
    output_path: Path | None = None,
    *,
    line_width: float = 0.9,
    draw_exercise_outlines: bool = True,
) -> tuple[Path, int, int]:
    """Copy *exam_pdf* with color-coded outlines for each scaffold region.

    Exercise ``bbox`` and figure ``images`` use distinct golden-ratio HSV colors per
    question (traversal order).  ``writing_areas`` are drawn on top in kind-specific
    colours (teal / yellow / cyan / magenta / orange).

    When *draw_exercise_outlines* is False, only the writing-area rects are drawn
    (used during calibration so per-question outlines don't visually clutter the
    answer-region overlay).

    Returns ``(output_path, rectangle_count, page_count)``.
    """
    exam_pdf = exam_pdf.resolve()
    if output_path is None:
        output_path = exam_pdf.with_name(f"{exam_pdf.stem}_raw_exam_bboxes.pdf")
    else:
        output_path = output_path.resolve()

    all_nodes = flatten_questions(questions)
    exercise_rects: list[tuple[int, fitz.Rect, tuple[float, float, float]]] = []
    writing_area_rects: list[tuple[int, fitz.Rect, tuple[float, float, float]]] = []

    for color_idx, node in enumerate(all_nodes):
        exr, war = _rects_for_question_node(node, color_idx)
        if draw_exercise_outlines:
            exercise_rects.extend(exr)
        writing_area_rects.extend(war)

    # Group by page; writing-area rects draw after exercise rects so they sit on top.
    by_page: dict[int, list[tuple[fitz.Rect, tuple[float, float, float]]]] = {}
    for page_1, rect, color in exercise_rects + writing_area_rects:
        by_page.setdefault(page_1, []).append((rect, color))

    doc = fitz.open(exam_pdf)
    try:
        for p1 in sorted(by_page.keys()):
            idx = p1 - 1
            if idx < 0 or idx >= len(doc):
                continue
            page = doc[idx]
            for rect, color in by_page[p1]:
                page.draw_rect(rect, color=color, width=line_width)
        doc.save(output_path, garbage=4, deflate=True)
    finally:
        doc.close()

    total_rects = len(exercise_rects) + len(writing_area_rects)
    pages_with_marks = len(by_page)
    return output_path, total_rects, pages_with_marks
