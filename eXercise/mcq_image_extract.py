# -*- coding: utf-8 -*-
"""MCQ image and text extraction from question-paper PDFs.

Detects questions that contain images or diagrams, rasterizes those image
zones for vision API use, and extracts plain question text from clip regions.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import TYPE_CHECKING

import fitz

from .config import SubjectConfig

_IMAGE_ZONE_PAD_V_PT = 5.0
_IMAGE_ZONE_PAD_H_PT = 8.0
_IMAGE_RASTER_DPI = 150

# Regex for answer-option labels: a single letter A–D optionally followed by
# a dot or parenthesis, possibly with surrounding whitespace.
_OPTION_LABEL_RE = re.compile(r"^\s*[A-D]\s*[.):]?\s*$")


def _is_substantial_drawing(r: fitz.Rect) -> bool:
    """Return True if a drawing rect is large enough to be a diagram element.

    Requires one dimension ≥20 pt and the other ≥12 pt.  This catches shapes
    like bar magnets (56×17 pt) while filtering out thin rules and borders.
    """
    lo, hi = min(r.width, r.height), max(r.width, r.height)
    return lo >= 12.0 and hi >= 20.0


def mcq_questions_with_images(
    doc: fitz.Document,
    regions: list[tuple[int, int, float, float]],
    questions: list[int],
    cfg: SubjectConfig,
) -> set[int]:
    """Return question numbers whose clip region contains at least one image.

    Checks embedded raster images and substantial vector drawings.
    Multi-page questions: any page containing an image counts the question.
    """
    has_image: set[int] = set()
    qs_set = set(questions)

    for qnum, page_idx, y_start, y_end in regions:
        if qnum not in qs_set or qnum in has_image:
            continue
        if page_idx >= len(doc):
            continue
        page = doc[page_idx]
        page_w = page.rect.width
        clip = fitz.Rect(
            cfg.strip_crop_left_pt,
            y_start + cfg.strip_crop_top_pt,
            page_w - cfg.strip_crop_right_pt,
            y_end,
        )

        # Check embedded raster images
        for img_item in page.get_images():
            xref = img_item[0]
            try:
                for rect in page.get_image_rects(xref):
                    if not fitz.Rect(rect).intersect(clip).is_empty:
                        has_image.add(qnum)
                        break
            except Exception:
                continue
            if qnum in has_image:
                break

        if qnum in has_image:
            continue

        # Check substantial vector drawings
        for drawing in page.get_drawings():
            r = drawing["rect"]
            if not _is_substantial_drawing(r):
                continue
            if not fitz.Rect(r).intersect(clip).is_empty:
                has_image.add(qnum)
                break

    return has_image


def _image_zone_clip(
    page: fitz.Page,
    question_clip: fitz.Rect,
) -> fitz.Rect:
    """Compute a tight clip around all images/drawings in *question_clip*.

    Strategy
    --------
    1. Find the union bbox of all raster images and substantial vector drawings.
    2. Scan text blocks for answer-option labels (A / B / C / D) that sit near
       the images (above or below) — these are included so the model knows
       which diagram belongs to which option.
    3. Add padding above and below.
    4. Fall back to the full *question_clip* when the zone already covers >90 %
       of the question height.

    The zone is NOT extended to the bottom of the question — the AI already
    receives the full question text separately, so the image only needs to
    capture the visual content and its labels.
    """
    img_y0 = question_clip.y1  # sentinel: bottom
    img_y1 = question_clip.y0  # sentinel: top
    img_x0 = question_clip.x1  # sentinel: right
    img_x1 = question_clip.x0  # sentinel: left

    for img_item in page.get_images():
        xref = img_item[0]
        try:
            for rect in page.get_image_rects(xref):
                if not fitz.Rect(rect).intersect(question_clip).is_empty:
                    img_y0 = min(img_y0, rect.y0)
                    img_y1 = max(img_y1, rect.y1)
                    img_x0 = min(img_x0, rect.x0)
                    img_x1 = max(img_x1, rect.x1)
        except Exception:
            continue

    # First pass: only substantial drawings set the vertical extent.
    clip_drawings: list[tuple[fitz.Rect, float]] = []  # (rect, half_stroke)
    for drawing in page.get_drawings():
        r = drawing["rect"]
        if fitz.Rect(r).intersect(question_clip).is_empty:
            continue
        half_w = (drawing.get("width") or 0) / 2
        clip_drawings.append((r, half_w))
        if _is_substantial_drawing(r):
            img_y0 = min(img_y0, r.y0 - half_w)
            img_y1 = max(img_y1, r.y1 + half_w)
            img_x0 = min(img_x0, r.x0 - half_w)
            img_x1 = max(img_x1, r.x1 + half_w)

    if img_y0 >= img_y1:
        return question_clip

    # Second pass: include all drawings (even small ones like arrows and
    # wires) that vertically overlap the zone established by substantial
    # drawings — they are part of the same figure.
    for r, half_w in clip_drawings:
        if (r.y1 + half_w) < img_y0 or (r.y0 - half_w) > img_y1:
            continue
        img_x0 = min(img_x0, r.x0 - half_w)
        img_x1 = max(img_x1, r.x1 + half_w)
        img_y0 = min(img_y0, r.y0 - half_w)
        img_y1 = max(img_y1, r.y1 + half_w)

    # Two kinds of text need to be included:
    #
    # a) Diagram labels — text whose vertical centre falls inside the
    #    drawing area (e.g. "3N", "X", "O").  These are part of the figure
    #    and the AI needs them to interpret the diagram.
    #
    # b) Answer-option labels (A / B / C / D) that sit near the images
    #    (up to 30 pt above or 25 pt below).  These tell the AI which
    #    diagram belongs to which option.
    _LABEL_LOOK_ABOVE_PT = 30.0
    _LABEL_LOOK_BELOW_PT = 25.0
    label_scan_top = max(question_clip.y0, img_y0 - _LABEL_LOOK_ABOVE_PT)
    label_scan_bot = min(question_clip.y1, img_y1 + _LABEL_LOOK_BELOW_PT)

    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        if block["bbox"][2] < question_clip.x0 or block["bbox"][0] > question_clip.x1:
            continue
        for line in block["lines"]:
            # Skip blank / whitespace-only lines — they sit between content
            # lines in the same block and would cascade the zone outward.
            if not any(s["text"].strip() for s in line["spans"]):
                continue
            ly0, ly1 = line["bbox"][1], line["bbox"][3]
            line_centre = (ly0 + ly1) / 2

            lx0, lx1 = line["bbox"][0], line["bbox"][2]

            # (a) Diagram label: line centre inside or within 15 pt of
            #     the drawing area (labels like "X", "O" sit just outside
            #     the drawing bbox at the corners of the figure).
            _DIAGRAM_LABEL_MARGIN_PT = 15.0
            if (img_y0 - _DIAGRAM_LABEL_MARGIN_PT) <= line_centre <= (img_y1 + _DIAGRAM_LABEL_MARGIN_PT):
                img_y0 = min(img_y0, ly0)
                img_y1 = max(img_y1, ly1)
                img_x0 = min(img_x0, lx0)
                img_x1 = max(img_x1, lx1)
                continue

            # (b) Option label (A–D) near the images
            if ly1 >= label_scan_top and ly0 <= label_scan_bot:
                line_text = "".join(s["text"] for s in line["spans"])
                if _OPTION_LABEL_RE.match(line_text):
                    img_y0 = min(img_y0, ly0)
                    img_y1 = max(img_y1, ly1)
                    img_x0 = min(img_x0, lx0)
                    img_x1 = max(img_x1, lx1)

    # Add small breathing room so images aren't cropped at the exact edge.
    zone_y0 = max(question_clip.y0, img_y0 - _IMAGE_ZONE_PAD_V_PT)
    zone_y1 = min(question_clip.y1, img_y1 + _IMAGE_ZONE_PAD_V_PT)
    zone_x0 = max(question_clip.x0, img_x0 - _IMAGE_ZONE_PAD_H_PT)
    zone_x1 = min(question_clip.x1, img_x1 + _IMAGE_ZONE_PAD_H_PT)

    # If zone already covers >90 % of the question, just use the full clip.
    q_h = question_clip.height
    if q_h > 0 and (zone_y1 - zone_y0) / q_h > 0.9:
        return question_clip

    return fitz.Rect(zone_x0, zone_y0, zone_x1, zone_y1)


def rasterize_mcq_images(
    doc: fitz.Document,
    regions: list[tuple[int, int, float, float]],
    questions_with_images: set[int],
    cfg: SubjectConfig,
    debug_dir: Path | None = None,
) -> dict[int, str]:
    """Rasterize the image zone for each question with images.

    Returns ``{qnum: base64_png}`` for every question in *questions_with_images*
    that has a detectable image zone.  For multi-page questions, uses the first
    region page that contains images.

    When *debug_dir* is provided, each rasterized image is also saved as
    ``<debug_dir>/Q<num>.png`` for visual inspection.
    """
    if debug_dir is not None:
        debug_dir.mkdir(parents=True, exist_ok=True)

    result: dict[int, str] = {}
    mat = fitz.Matrix(_IMAGE_RASTER_DPI / 72, _IMAGE_RASTER_DPI / 72)

    for qnum, page_idx, y_start, y_end in regions:
        if qnum not in questions_with_images or qnum in result:
            continue
        if page_idx >= len(doc):
            continue

        page = doc[page_idx]
        page_w = page.rect.width
        question_clip = fitz.Rect(
            cfg.strip_crop_left_pt,
            y_start + cfg.strip_crop_top_pt,
            page_w - cfg.strip_crop_right_pt,
            y_end,
        )

        zone = _image_zone_clip(page, question_clip)
        pix = page.get_pixmap(matrix=mat, clip=zone)
        png_bytes = pix.tobytes("png")

        if debug_dir is not None:
            (debug_dir / f"Q{qnum}.png").write_bytes(png_bytes)

        result[qnum] = base64.b64encode(png_bytes).decode("ascii")

    return result


def extract_mcq_question_texts(
    doc: fitz.Document,
    regions: list[tuple[int, int, float, float]],
    questions: list[int],
    cfg: SubjectConfig,
) -> dict[int, str]:
    """Return plain text for each requested question, extracted from the clip region.

    Mirrors the portrait QP clip used by ``collect_vector_strips``:
    ``clip_x0 = cfg.strip_crop_left_pt``, ``clip_x1 = page_w - cfg.strip_crop_right_pt``,
    ``clip_y0 = y_start + cfg.strip_crop_top_pt``, ``clip_y1 = y_end``.

    Multi-page questions are concatenated with a space.
    """
    texts: dict[int, list[str]] = {q: [] for q in questions}
    for qnum, page_idx, y_start, y_end in regions:
        if qnum not in texts:
            continue
        if page_idx >= len(doc):
            continue
        page = doc[page_idx]
        page_w = page.rect.width
        clip = fitz.Rect(
            cfg.strip_crop_left_pt,
            y_start + cfg.strip_crop_top_pt,
            page_w - cfg.strip_crop_right_pt,
            y_end,
        )
        raw = page.get_text("text", clip=clip).strip()
        if raw:
            texts[qnum].append(raw)

    return {q: "\n".join(parts) for q, parts in texts.items() if parts}
