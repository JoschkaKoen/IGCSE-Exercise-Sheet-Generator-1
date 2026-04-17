# -*- coding: utf-8 -*-
"""Vector PDF assembly: clip source pages with show_pdf_page, no rasterisation."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any

import fitz

from .config import (
    A4_HEIGHT_PT,
    A4_WIDTH_PT,
    DEFAULT_SUBJECT_CONFIG,
    EXAM_LABEL_FONT_PT,
    EXAM_LABEL_TOP_PT,
    OUTPUT_MARGIN_PT,
    OUTPUT_MARGIN_RIGHT_PT,
    MS_LANDSCAPE_H_THRESHOLD_PT,
    MS_LANDSCAPE_MARGIN_PT,
    MS_MARKS_START_PT,
    MS_PORTRAIT_MARKS_START_PT,
    MS_PORTRAIT_TABLE_LEFT_PT,
    MS_TABLE_LEFT_PT,
    QR_MARGIN_ZONE_PT,
    QR_MAX_SIZE_PT,
    SubjectConfig,
)
from .mark_scheme import (
    detect_landscape_ms_crop_x,
    detect_landscape_ms_table_left,
    detect_portrait_ms_crop_x,
    detect_portrait_ms_table_left,
)

# ---------------------------------------------------------------------------
# Strip types
# ---------------------------------------------------------------------------

@dataclass
class VectorStrip:
    """A rectangular region of a source PDF page to embed via show_pdf_page."""
    src_doc: fitz.Document
    page_idx: int
    clip_rect: fitz.Rect       # source PDF coordinates (points)
    display_h_pt: float         # rendered height on the output page
    display_w_pt: float         # rendered width on the output page
    x_offset_pt: float          # left edge on the output page
    qr_rects: list[fitz.Rect] = field(default_factory=list)  # source-space embedded-image rects to white-out
    question_num: int | None = None  # source question number (exercise sheet nav anchors)
    extra_question_nums: list[int] = field(default_factory=list)  # additional questions sharing this strip's anchor


@dataclass
class McqStrip:
    """MCQ answer block rendered as native PDF text (no PIL)."""
    lines: list[tuple[str, bool]]  # (text, is_bold)
    display_h_pt: float


@dataclass
class GapStrip:
    """Vertical whitespace between content blocks."""
    height_pt: float


Strip = VectorStrip | McqStrip | GapStrip | str

_MARGIN_PT = float(OUTPUT_MARGIN_PT)
_MARGIN_RIGHT_PT = float(OUTPUT_MARGIN_RIGHT_PT)
_USABLE_W_PT = A4_WIDTH_PT - _MARGIN_PT - _MARGIN_RIGHT_PT

# Extra space below the MCQ answer-sheet headline before Q1, Q2, … (PDF points).
_MCQ_AFTER_TITLE_GAP_PT = 2.0
# "Multiple Choice Answers" — slightly smaller than the Q-lines.
_MCQ_TITLE_FONT_PT = 11.0
# Vertical advance for the title row (PDF points).
_MCQ_TITLE_LINE_PT = 16.0

# ---------------------------------------------------------------------------
# QR detection (image-rect only — no pixel heuristic)
# ---------------------------------------------------------------------------

def collect_qr_image_rects(page: fitz.Page) -> list[fitz.Rect]:
    """Return source-space bounding rects of embedded images that look like QR codes.

    Only uses PDF image metadata (no rasterisation).  Applies the same size/
    aspect/margin filters as the old blank_qr_codes_on_page pixel path.
    """
    pw, ph = page.rect.width, page.rect.height
    rects: list[fitz.Rect] = []
    try:
        for img_item in page.get_images():
            xref = img_item[0]
            try:
                img_rects = page.get_image_rects(xref)
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.debug("get_image_rects xref=%s: %s", xref, exc)
                continue
            for rect in img_rects:
                iw, ih = rect.width, rect.height
                if iw < 5 or ih < 5:
                    continue
                if iw > QR_MAX_SIZE_PT or ih > QR_MAX_SIZE_PT:
                    continue
                if max(iw, ih) / min(iw, ih) > 2.0:
                    continue
                in_margin = (
                    rect.x0 < QR_MARGIN_ZONE_PT
                    or rect.x1 > pw - QR_MARGIN_ZONE_PT
                    or rect.y0 < QR_MARGIN_ZONE_PT
                    or rect.y1 > ph - QR_MARGIN_ZONE_PT
                )
                if in_margin:
                    rects.append(fitz.Rect(rect))
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.debug("collect_qr_image_rects failed: %s", exc)
    return rects


def collect_barcode_text_rects(page: fitz.Page) -> list[fitz.Rect]:
    """Return padded bounding rects of text spans rendered with barcode fonts.

    Cambridge papers (2024+) encode page barcodes as text using special
    fonts (e.g. AllAndNone2) rather than embedded images.  These must be
    blanked just like image-based QR codes.

    Barcode glyphs render well beyond the reported text bbox (~6 pt above
    the top edge).  Padding is asymmetric: generous above (where bars
    extend beyond the bbox), minimal below (question text starts only
    ~4.7 pt below the barcode bbox bottom).  The layout code clamps each
    blank rect to the strip's target area so it can never overwrite the
    header band.  Horizontal pad is kept small because the question
    number (x ≈ 50) sits just left of the barcode (x ≈ 66).  Only spans
    near a page edge (within ``QR_MARGIN_ZONE_PT``) are considered,
    mirroring the safety filter on image-based QR detection.
    """
    _BARCODE_FONT_MARKERS = ("AllAndNone2",)
    _PAD_X = 2.0
    _PAD_TOP = 8.0   # barcode bars extend well above text bbox
    _PAD_BOTTOM = 2.0  # question text is only ~4.7 pt below bbox bottom
    pw, ph = page.rect.width, page.rect.height
    rects: list[fitz.Rect] = []
    try:
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if not any(m in span["font"] for m in _BARCODE_FONT_MARKERS):
                        continue
                    x0, y0, x1, y1 = span["bbox"]
                    in_margin = (
                        x0 < QR_MARGIN_ZONE_PT
                        or x1 > pw - QR_MARGIN_ZONE_PT
                        or y0 < QR_MARGIN_ZONE_PT
                        or y1 > ph - QR_MARGIN_ZONE_PT
                    )
                    if in_margin:
                        rects.append(fitz.Rect(
                            x0 - _PAD_X, y0 - _PAD_TOP,
                            x1 + _PAD_X, y1 + _PAD_BOTTOM,
                        ))
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.debug("collect_barcode_text_rects failed: %s", exc)
    return rects


def _display_to_mediabox(rect: fitz.Rect, page: fitz.Page) -> fitz.Rect:
    """Convert a display-space rect to mediabox-space (pre-rotation coordinates)."""
    rot = page.rotation % 360
    if rot == 0:
        return rect
    x0, y0, x1, y1 = rect.x0, rect.y0, rect.x1, rect.y1
    if rot == 90:
        h = page.mediabox.height
        return fitz.Rect(y0, h - x1, y1, h - x0)
    if rot == 270:
        w = page.mediabox.width
        return fitz.Rect(w - y1, x0, w - y0, x1)
    if rot == 180:
        w, h = page.mediabox.width, page.mediabox.height
        return fitz.Rect(w - x1, h - y1, w - x0, h - y0)
    return rect


# ---------------------------------------------------------------------------
# Derotated page cache
# ---------------------------------------------------------------------------
# show_pdf_page silently drops some text when embedding rotated pages via the
# XObject route.  The reliable workaround is insert_pdf (preserves all content),
# then set_rotation(0) to strip the /Rotate flag, and finally clip from the
# derotated copy using MediaBox coordinates + an explicit rotate parameter.

_derotated_cache: dict[tuple[int, int], tuple[fitz.Document, int, int]] = {}
"""(id(doc), page_idx) → (temp_doc, temp_page_idx, original_rotation)."""
_derotated_cache_lock = threading.Lock()


def _get_derotated(doc: fitz.Document, page_idx: int) -> tuple[fitz.Document, int, int]:
    """Return a (temp_doc, temp_page_idx, original_rotation) for *page_idx*.

    For rotation==0 pages, returns the original doc/page unchanged.
    For rotated pages, copies the page via insert_pdf and sets rotation to 0.
    """
    page = doc[page_idx]
    rot = page.rotation % 360
    if rot == 0:
        return doc, page_idx, 0

    key = (id(doc), page_idx)
    with _derotated_cache_lock:
        if key in _derotated_cache:
            return _derotated_cache[key]

        temp = fitz.open()
        temp.insert_pdf(doc, from_page=page_idx, to_page=page_idx)
        temp[0].set_rotation(0)
        _derotated_cache[key] = (temp, 0, rot)
        return temp, 0, rot


def clear_derotated_cache() -> None:
    """Close all temp documents in the cache (call after layout is done)."""
    with _derotated_cache_lock:
        for temp_doc, _, _ in _derotated_cache.values():
            try:
                temp_doc.close()
            except Exception:
                pass
        _derotated_cache.clear()


def _map_source_to_output(
    src_rect: fitz.Rect,
    clip: fitz.Rect,
    target: fitz.Rect,
) -> fitz.Rect:
    """Map a rect in source-page coordinates to the corresponding output rect.

    The transformation is the affine map clip → target (scale + translate).
    """
    if clip.width == 0 or clip.height == 0:
        return fitz.Rect(target)
    sx = target.width / clip.width
    sy = target.height / clip.height
    x0 = target.x0 + (src_rect.x0 - clip.x0) * sx
    y0 = target.y0 + (src_rect.y0 - clip.y0) * sy
    x1 = target.x0 + (src_rect.x1 - clip.x0) * sx
    y1 = target.y0 + (src_rect.y1 - clip.y0) * sy
    return fitz.Rect(x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# Collect strips from extracted regions
# ---------------------------------------------------------------------------

def collect_vector_strips(
    doc: fitz.Document,
    regions: list[tuple[int, int, float, float]],
    is_ms: bool = False,
    cfg: SubjectConfig | None = None,
) -> list[Strip]:
    """Build a list of VectorStrip / GapStrip objects from (qnum, page_idx, y_start, y_end) tuples.

    All geometry is in PDF points; no rasterisation occurs here.
    """
    cfg = cfg or DEFAULT_SUBJECT_CONFIG

    landscape_crop_x = MS_MARKS_START_PT
    landscape_table_left = MS_TABLE_LEFT_PT
    portrait_crop_x = MS_PORTRAIT_MARKS_START_PT
    portrait_table_left = MS_PORTRAIT_TABLE_LEFT_PT
    if is_ms:
        detected_l = detect_landscape_ms_crop_x(doc)
        if detected_l is not None:
            landscape_crop_x = detected_l
        detected_ll = detect_landscape_ms_table_left(doc)
        if detected_ll is not None:
            landscape_table_left = detected_ll
        detected_p = detect_portrait_ms_crop_x(doc)
        if detected_p is not None:
            portrait_crop_x = detected_p
        detected_pl = detect_portrait_ms_table_left(doc)
        if detected_pl is not None:
            portrait_table_left = detected_pl

    # Pre-collect QR / barcode rects per needed page
    needed_pages = set(r[1] for r in regions)
    qr_by_page: dict[int, list[fitz.Rect]] = {
        pi: collect_qr_image_rects(doc[pi]) + collect_barcode_text_rects(doc[pi])
        for pi in needed_pages
    }

    strips: list[Strip] = []
    current_qnum: int | None = None

    for qnum, page_idx, y_start, y_end in regions:
        page = doc[page_idx]
        page_w = page.rect.width
        page_h = page.rect.height

        is_landscape = page_h < MS_LANDSCAPE_H_THRESHOLD_PT

        if is_landscape:
            # Landscape mark-scheme page
            clip_x0 = landscape_table_left
            clip_x1 = landscape_crop_x
            clip_y0 = y_start
            clip_y1 = y_end
            _ms_margin = cfg.ms_answer_landscape_margin_pt if cfg.ms_answer_landscape_margin_pt is not None else MS_LANDSCAPE_MARGIN_PT
            display_w = A4_WIDTH_PT - 2 * _ms_margin
            x_offset = (A4_WIDTH_PT - display_w) / 2
        elif is_ms:
            # Portrait mark-scheme page
            clip_x0 = portrait_table_left
            clip_x1 = portrait_crop_x
            clip_y0 = y_start
            clip_y1 = y_end
            clip_w = clip_x1 - clip_x0
            if cfg.ms_answer_portrait_margin_pt is not None:
                # Scale table to fill the target width, centred.
                display_w = A4_WIDTH_PT - 2 * cfg.ms_answer_portrait_margin_pt
            else:
                # Native (1:1) width — for slim tables (mathematics / CS paper-22).
                display_w = clip_w
            x_offset = (A4_WIDTH_PT - display_w) / 2
        else:
            # Portrait question-paper page
            clip_x0 = cfg.strip_crop_left_pt
            clip_x1 = page_w - cfg.strip_crop_right_pt
            # Remove the padding_above blank so every strip starts at the
            # question number with no leading whitespace.  Cap the crop to
            # however far y_start actually sits above margin_top: when the
            # strip is clamped to margin_top (question near page top) or is
            # a multi-page continuation (y_start == margin_top exactly),
            # there is no blank to remove and a full strip_crop_top_pt crop
            # would slice into real content, cutting off the first text line.
            _available = max(0.0, y_start - cfg.margin_top)
            clip_y0 = y_start + min(cfg.strip_crop_top_pt, _available)
            clip_y1 = y_end
            display_w = _USABLE_W_PT
            x_offset = _MARGIN_PT

        clip_rect = fitz.Rect(clip_x0, clip_y0, clip_x1, clip_y1)
        clip_w = clip_rect.width
        clip_h = clip_rect.height
        if clip_w <= 0 or clip_h <= 0:
            continue

        display_h = clip_h * (display_w / clip_w)

        # Filter QR rects to those overlapping this clip
        page_qrs = [r for r in qr_by_page.get(page_idx, [])
                    if not fitz.Rect(r).intersect(clip_rect).is_empty]

        # Uniform separator between different questions.
        if current_qnum is not None and qnum != current_qnum:
            strips.append(GapStrip(height_pt=6.0 if is_ms else 16.0))
        elif current_qnum is not None and qnum == current_qnum and is_ms:
            # Same question continuing on the next source page: overlap the
            # continuation strip with the previous one so the duplicated
            # border lines merge seamlessly.  The previous strip ends with
            # drawing_bottom_pad_pt of whitespace below its last border;
            # the continuation starts at its first border (0.48 pt thick).
            # Overlap by pad + border thickness so the second border is
            # drawn on top of the first one.
            _overlap = cfg.drawing_bottom_pad_pt + 0.48
            strips.append(GapStrip(height_pt=-_overlap))

        strips.append(VectorStrip(
            src_doc=doc,
            page_idx=page_idx,
            clip_rect=clip_rect,
            display_h_pt=display_h,
            display_w_pt=display_w,
            x_offset_pt=x_offset,
            qr_rects=page_qrs,
            question_num=qnum,
        ))
        current_qnum = qnum

    return strips


# ---------------------------------------------------------------------------
# Header drawing
# ---------------------------------------------------------------------------

_LABEL_FS = float(EXAM_LABEL_FONT_PT)
_LABEL_H = _LABEL_FS + 8.0            # total band height: 4pt pad + font + 4pt pad
_LABEL_BASELINE_OFF = _LABEL_FS + 4.0  # baseline offset from band top
_LABEL_TOP_PT = float(EXAM_LABEL_TOP_PT)  # distance from page top to the label band
_LABEL_GAP_PT = 6.0                    # gap after the top-of-page header band → first exercise
_INLINE_LABEL_GAP_PT = 6.0             # gap after an inline mid-page label → following exercise

# Name field (left side of header band)
_NAME_BOX_PAD_X  = 5.0   # left padding (start of "Name: " label)
_NAME_BOX_SHIFT_X = 1.0  # extra offset to the right for write-in box only (gap after label)
_NAME_BOX_W      = 90.0  # writeable box width in points
_NAME_BOX_CORNER = 2.5   # desired corner radius in points (converted to relative inside draw)
_NAME_BOX_PAD_Y  = 1.0   # vertical padding above/below text within the box

import functools as _functools


@_functools.cache
def _helv_metrics() -> tuple[float, float]:
    """Return (ascender, abs(descender)) for the built-in Helvetica font (lazy, cached)."""
    f = fitz.Font("helv")
    return f.ascender, abs(f.descender)


def _header_text(subject_label: str, paper_label: str | None) -> str:
    """Combine subject + paper into a single centred label."""
    if paper_label:
        return f"{subject_label}: {paper_label}"
    return subject_label


def _draw_label(out_page: fitz.Page, text: str, y: float) -> None:
    """Draw a '─── label ───' style centred label with the band top at *y*.

    The text is expected to be "Subject: paper" (produced by ``_header_text``).
    The subject part is rendered in regular weight; the paper number is bold.
    If no ": " separator is present the whole string is rendered regular.

    Used for both the top-of-page header and inline paper-section dividers.
    Font size is driven by ``EXAM_LABEL_FONT_PT`` in config.
    """
    fs = _LABEL_FS
    col = (0, 0, 0)
    baseline_y = y + _LABEL_BASELINE_OFF

    if ": " in text:
        sep = ": "
        subject, paper = text.split(": ", 1)
        prefix = subject + sep          # regular part  e.g. "IGCSE Physics: "
        w_prefix = fitz.get_text_length(prefix, fontname="helv", fontsize=fs)
        w_paper  = fitz.get_text_length(paper,  fontname="hebo", fontsize=fs)
        text_w = w_prefix + w_paper
        x_text = (A4_WIDTH_PT - text_w) / 2
        out_page.insert_text(fitz.Point(x_text, baseline_y),
                             prefix, fontsize=fs, fontname="helv",
                             color=col, render_mode=0)
        out_page.insert_text(fitz.Point(x_text + w_prefix, baseline_y),
                             paper, fontsize=fs, fontname="hebo",
                             color=col, render_mode=0)
    else:
        text_w = fitz.get_text_length(text, fontname="helv", fontsize=fs)
        x_text = (A4_WIDTH_PT - text_w) / 2
        out_page.insert_text(fitz.Point(x_text, baseline_y),
                             text, fontsize=fs, fontname="helv",
                             color=col, render_mode=0)

    line_y = baseline_y - fs * 0.35
    pad = 8.0
    line_col = (0, 0, 0)
    if x_text > _MARGIN_PT + pad + 10:
        out_page.draw_line(
            fitz.Point(_MARGIN_PT + pad, line_y),
            fitz.Point(x_text - pad, line_y),
            color=line_col, width=0.5,
        )
        out_page.draw_line(
            fitz.Point(x_text + text_w + pad, line_y),
            fitz.Point(A4_WIDTH_PT - _MARGIN_RIGHT_PT - pad, line_y),
            color=line_col, width=0.5,
        )


def _draw_header_line(out_page: fitz.Page, text: str) -> None:
    """Draw the top-of-page label band (band top = _LABEL_TOP_PT)."""
    _draw_label(out_page, text, _LABEL_TOP_PT)


def _erase_header_band(out_page: fitz.Page) -> None:
    """White-out the header band before redrawing with an updated label."""
    out_page.draw_rect(
        fitz.Rect(0, 0, A4_WIDTH_PT, _LABEL_TOP_PT + _LABEL_H + 1),
        fill=(1, 1, 1), color=(1, 1, 1),
    )


_INLINE_LABEL_H = _LABEL_H   # vertical space consumed by an inline paper divider


def _draw_inline_paper_label(out_page: fitz.Page, label: str, y: float) -> None:
    """Draw an inline paper-section divider (delegates to ``_draw_label``)."""
    _draw_label(out_page, label, y)


def _draw_name_box(out_page: fitz.Page) -> None:
    """Draw 'Name: [___]' in the left portion of the top-of-page header band.

    The text centre and the box centre are both placed exactly on the IGCSE
    decorative line (the same y as the lines flanking the centred label).
    Font metrics are taken from fitz so the positioning is exact regardless of
    the chosen font size.
    """
    fs  = _LABEL_FS
    x0  = _MARGIN_PT + _NAME_BOX_PAD_X   # ≈ 15 pt from left edge

    # IGCSE decorative line sits at this y (same formula as _draw_label)
    line_y = _LABEL_TOP_PT + _LABEL_BASELINE_OFF - _LABEL_FS * 0.35

    # Actual ascender / descender for Helvetica at this font size
    _helv_asc, _helv_dsc = _helv_metrics()
    asc = _helv_asc * fs   # ≈ 9.68 pt  (above baseline)
    dsc = _helv_dsc * fs   # ≈ 2.69 pt  (below baseline)

    # Baseline that places the optical midpoint of "Name:" on line_y.
    # Full metric centering is (asc-dsc)/2, but "Name:" has no real
    # descenders and the ascender metric includes diacritic headroom,
    # so the text looks low — lift by 0.5 pt for optical balance.
    baseline_y = line_y + (asc - dsc) / 2 - 0.5

    # Box spans the full line-height + equal padding on each side
    half_box = (asc + dsc) / 2 + _NAME_BOX_PAD_Y
    box_y0   = line_y - half_box
    box_y1   = line_y + half_box

    label  = "Name: "
    w_label = fitz.get_text_length(label, fontname="helv", fontsize=fs)
    box_x0  = x0 + w_label + _NAME_BOX_SHIFT_X

    # radius in draw_rect is relative [0, 1]: 1 = half the shorter side
    half_h = (box_y1 - box_y0) / 2
    rel_r  = min(_NAME_BOX_CORNER / half_h, 0.99)

    # Erase the decorative line across the full label+box area
    out_page.draw_rect(
        fitz.Rect(_MARGIN_PT, box_y0, box_x0 + _NAME_BOX_W, box_y1),
        color=(1, 1, 1), fill=(1, 1, 1), width=0,
    )
    # Draw the rounded write-in box
    out_page.draw_rect(
        fitz.Rect(box_x0, box_y0, box_x0 + _NAME_BOX_W, box_y1),
        color=(0, 0, 0), fill=None, width=0.5,
        radius=rel_r,
    )
    # "Name: " label — baseline derived so text is centred on line_y
    out_page.insert_text(
        fitz.Point(x0, baseline_y),
        label,
        fontsize=fs, fontname="helv", color=(0, 0, 0), render_mode=0,
    )


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------

def layout_vector_strips_to_pdf(
    strips: list[Strip],
    output_path: str,
    header_label: str | None = None,
    *,
    paper_always_newpage: bool = False,
    page_number_circle: bool = True,
    page_number_raise: float = 2 / 3,
    name_field: bool = False,
) -> list[dict[str, Any]]:
    """Flow strips onto A4 pages and write a vector PDF.

    Strips are VectorStrip (show_pdf_page), McqStrip (insert_text),
    GapStrip (whitespace), or str (paper sub-label).

    When *paper_always_newpage* is True every paper sub-label (``str`` strip)
    starts a fresh page if any content has already been placed.  This ensures
    each paper's section gets its own page with the correct ``subject: paper``
    header — used for answer sheets where space is rarely a constraint.

    Returns navigation anchors for the exercise sheet: each dict has ``paper`` (str
    or null), ``q`` (int), ``page`` (0-based output page index), ``y_pt`` (distance
    from top of that page to the top of the question strip, in PDF points), and
    ``y_view_pt`` (PDF y to align with the viewport top when scrolling — includes the
    inline paper divider when one sits directly above the exercise).

    *page_number_circle* (default ``True``) draws a thin circle around the bold
    centred page number at the bottom of each page.  Set to ``False`` to show the
    number without any enclosing shape.

    *name_field* (default ``False``) draws a ``Name: [___]`` label and rounded
    write-in box in the top-left of every page's header band.  Enable for
    exercise sheets; leave off for mark schemes.
    """
    hl = (header_label or "").strip() or None

    # Determine initial paper label (first str in strips, if any)
    current_paper_label: str | None = None
    for item in strips:
        if isinstance(item, str):
            current_paper_label = item
            break

    has_header = bool(hl or current_paper_label)
    initial_y_pt = (_LABEL_TOP_PT + _LABEL_H + _LABEL_GAP_PT) if has_header else _MARGIN_PT
    usable_h_pt = A4_HEIGHT_PT - _MARGIN_PT - initial_y_pt

    out_doc = fitz.open()

    # page_first_paper_label tracks the paper label of the FIRST content block on
    # the current page.  The top-of-page header always shows "Subject: first_paper"
    # even when content from multiple papers shares the page.
    page_first_paper_label: str | None = current_paper_label

    inline_label_above_exercise = False

    def new_page() -> tuple[fitz.Page, float]:
        nonlocal page_first_paper_label, inline_label_above_exercise
        inline_label_above_exercise = False
        page_first_paper_label = current_paper_label
        pg = out_doc.new_page(width=A4_WIDTH_PT, height=A4_HEIGHT_PT)
        if has_header:
            _draw_header_line(pg, _header_text(hl or "", page_first_paper_label))
            if name_field:
                _draw_name_box(pg)
        return pg, initial_y_pt

    def redraw_header(pg: fitz.Page) -> None:
        _erase_header_band(pg)
        if has_header:
            _draw_header_line(pg, _header_text(hl or "", page_first_paper_label))
            if name_field:
                _draw_name_box(pg)

    current_page, y_cursor = new_page()

    anchors: list[dict[str, Any]] = []
    anchor_seen: set[tuple[str | None, int]] = set()

    def _record_exercise_anchor(vstrip: VectorStrip, page: fitz.Page, y_top: float) -> None:
        nonlocal inline_label_above_exercise
        qn = vstrip.question_num
        if qn is None:
            inline_label_above_exercise = False
            return
        all_qnums = [qn] + list(vstrip.extra_question_nums)
        first_new = True
        for q in all_qnums:
            key = (current_paper_label, q)
            if key in anchor_seen:
                continue
            anchor_seen.add(key)
            if first_new and inline_label_above_exercise:
                y_view = max(
                    0.0,
                    y_top - _INLINE_LABEL_H - _INLINE_LABEL_GAP_PT,
                )
            elif first_new and has_header and abs(y_top - initial_y_pt) < 1.0:
                y_view = float(_LABEL_TOP_PT)
            else:
                y_view = float(y_top)
            anchors.append(
                {
                    "paper": current_paper_label,
                    "q": int(q),
                    "page": int(page.number),
                    "y_pt": float(y_top),
                    "y_view_pt": y_view,
                }
            )
            first_new = False
        inline_label_above_exercise = False

    def _next_content_h(idx: int) -> float:
        """Height of the first VectorStrip/McqStrip after *idx* (skips gaps/labels)."""
        for i in range(idx + 1, len(strips)):
            s = strips[i]
            if isinstance(s, (VectorStrip, McqStrip)):
                return s.display_h_pt
            if isinstance(s, (GapStrip, str)):
                continue
        return 0.0

    for strip_idx, item in enumerate(strips):

        # --- paper sub-label (str) ---
        if isinstance(item, str):
            current_paper_label = item
            if y_cursor == initial_y_pt:
                # Still at top of page — update the header and the first-paper tracker.
                page_first_paper_label = current_paper_label
                redraw_header(current_page)
            elif paper_always_newpage:
                current_page, y_cursor = new_page()
            else:
                remaining = A4_HEIGHT_PT - _MARGIN_PT - y_cursor
                # Look ahead: would the first following exercise also fit after the label?
                next_h = _next_content_h(strip_idx)
                fits = (next_h == 0 or
                        y_cursor + _INLINE_LABEL_H + _INLINE_LABEL_GAP_PT + next_h <= A4_HEIGHT_PT - _MARGIN_PT)
                if remaining < _INLINE_LABEL_H + 40 or not fits:
                    # Not enough room for the divider + the next exercise — new page.
                    current_page, y_cursor = new_page()
                else:
                    # Draw "IGCSE Subject: paper_code" as an inline section header
                    # and continue flowing content on the same page.
                    inline_lbl = _header_text(hl or "", current_paper_label)
                    _draw_inline_paper_label(current_page, inline_lbl, y_cursor)
                    y_cursor += _INLINE_LABEL_H + _INLINE_LABEL_GAP_PT
                    inline_label_above_exercise = True
            continue

        # --- gap ---
        if isinstance(item, GapStrip):
            y_cursor += item.height_pt
            continue

        # --- MCQ text block ---
        if isinstance(item, McqStrip):
            sh = item.display_h_pt
            if y_cursor + sh > A4_HEIGHT_PT - _MARGIN_PT:
                current_page, y_cursor = new_page()
            line_h = 16.0
            bold_fs = 14.0
            reg_fs = 11.0
            x_left = 50.0
            for i, (text, is_bold) in enumerate(item.lines):
                if i == 0 and is_bold:
                    fs = _MCQ_TITLE_FONT_PT
                    current_page.insert_text(
                        fitz.Point(x_left, y_cursor + fs),
                        text,
                        fontsize=fs,
                        color=(0.0, 0.0, 0.0),
                        fontname="hebo",
                    )
                    y_cursor += _MCQ_TITLE_LINE_PT + _MCQ_AFTER_TITLE_GAP_PT
                else:
                    fs = bold_fs if is_bold else reg_fs
                    current_page.insert_text(
                        fitz.Point(x_left, y_cursor + fs),
                        text,
                        fontsize=fs,
                        color=(0.0, 0.0, 0.0),
                        fontname="hebo" if is_bold else "helv",
                    )
                    y_cursor += line_h if not is_bold else line_h + 2
            continue

        # --- vector content strip ---
        if isinstance(item, VectorStrip):
            sh = item.display_h_pt
            clip = item.clip_rect               # display-space rect
            src_page = item.src_doc[item.page_idx]

            # For rotated pages, use the insert_pdf derotation workaround:
            # show_pdf_page silently drops some text when embedding rotated
            # pages directly.  _get_derotated gives us a rotation=0 copy.
            derot_doc, derot_pi, orig_rot = _get_derotated(
                item.src_doc, item.page_idx
            )
            mb_clip = _display_to_mediabox(clip, src_page)
            # (360 - rot) % 360 in CW convention matches the original /Rotate
            show_rot = (360 - orig_rot) % 360

            scale_x = item.display_w_pt / clip.width if clip.width > 0 else 1.0

            if y_cursor + sh > A4_HEIGHT_PT - _MARGIN_PT:
                if sh > usable_h_pt:
                    # Tall strip: chunk across pages.
                    # Always start on a fresh page so the first chunk gets
                    # maximum space — avoids tiny orphan slivers (e.g. half
                    # a figure) at the bottom of the previous page.
                    if y_cursor > initial_y_pt + 1.0:
                        current_page, y_cursor = new_page()
                    src_y0 = clip.y0
                    src_remaining = clip.height
                    chunk_first = True
                    while src_remaining > 0:
                        available_pt = A4_HEIGHT_PT - _MARGIN_PT - y_cursor
                        # Avoid orphan slivers: require at least 120 pt
                        # (~4 cm) of space before starting a chunk at the
                        # bottom of a page; otherwise push to a fresh page.
                        _MIN_CHUNK_PT = 120.0
                        if available_pt < _MIN_CHUNK_PT:
                            current_page, y_cursor = new_page()
                            available_pt = A4_HEIGHT_PT - _MARGIN_PT - y_cursor

                        src_chunk_h = min(src_remaining, available_pt / scale_x)
                        # If the leftover after this chunk would be tiny
                        # (< 40 pt in source space ≈ 1 empty answer line),
                        # absorb it into the current chunk rather than
                        # pushing a near-empty sliver onto a new page.
                        _MIN_LEFTOVER_SRC_PT = 40.0
                        leftover = src_remaining - src_chunk_h
                        if 0 < leftover < _MIN_LEFTOVER_SRC_PT:
                            src_chunk_h = src_remaining
                        chunk_display = fitz.Rect(
                            clip.x0, src_y0,
                            clip.x1, src_y0 + src_chunk_h,
                        )
                        chunk_mb = _display_to_mediabox(chunk_display, src_page)
                        out_h = src_chunk_h * scale_x
                        if chunk_first:
                            _record_exercise_anchor(item, current_page, y_cursor)
                            chunk_first = False
                        target = fitz.Rect(
                            item.x_offset_pt, y_cursor,
                            item.x_offset_pt + item.display_w_pt, y_cursor + out_h,
                        )
                        current_page.show_pdf_page(
                            target, derot_doc, derot_pi,
                            clip=chunk_mb, rotate=show_rot,
                        )
                        for qr in item.qr_rects:
                            mapped = _map_source_to_output(qr, chunk_display, target)
                            mapped &= target  # clamp to strip area
                            if not mapped.is_empty:
                                current_page.draw_rect(mapped, fill=(1,1,1), color=(1,1,1))
                        y_cursor += out_h
                        src_y0 += src_chunk_h
                        src_remaining -= src_chunk_h
                        if src_remaining > 0:
                            current_page, y_cursor = new_page()
                    continue
                else:
                    current_page, y_cursor = new_page()

            _record_exercise_anchor(item, current_page, y_cursor)
            target = fitz.Rect(
                item.x_offset_pt, y_cursor,
                item.x_offset_pt + item.display_w_pt, y_cursor + sh,
            )
            current_page.show_pdf_page(
                target, derot_doc, derot_pi,
                clip=mb_clip, rotate=show_rot,
            )
            for qr in item.qr_rects:
                mapped = _map_source_to_output(qr, clip, target)
                mapped &= target  # clamp to strip area
                if not mapped.is_empty:
                    current_page.draw_rect(mapped, fill=(1,1,1), color=(1,1,1))
            y_cursor += sh

    # Add bold centred page numbers at the bottom of every page.
    _pagenum_fs = _LABEL_FS
    _pagenum_pad_x = 4.0   # horizontal clearance between text and circle edge
    _pagenum_pad_y = 2.5   # vertical clearance above baseline / below cap-height
    for pg in out_doc:
        label = str(pg.number + 1)
        w = fitz.get_text_length(label, fontname="hebo", fontsize=_pagenum_fs)
        x = (A4_WIDTH_PT - w) / 2
        baseline_y = A4_HEIGHT_PT - _MARGIN_PT
        cap_h = _pagenum_fs * 0.7   # approximate cap-height
        cx = A4_WIDTH_PT / 2
        cy = baseline_y - cap_h / 2
        rx = w / 2 + _pagenum_pad_x
        ry = cap_h / 2 + _pagenum_pad_y
        r = max(rx, ry)             # use a circle (equal radii)
        offset = page_number_raise * r
        cy -= offset
        baseline_y -= offset
        # Draw the thin circle *before* the text so the text sits on top.
        if page_number_circle:
            pg.draw_circle(
                fitz.Point(cx, cy), r,
                color=(0, 0, 0), fill=None, width=0.5,
            )
        pg.insert_text(
            fitz.Point(x, baseline_y),
            label,
            fontsize=_pagenum_fs,
            fontname="hebo",
            color=(0, 0, 0),
            render_mode=0,
        )

    print(f"  Assembling {len(out_doc)} output page(s)...")
    out_doc.save(output_path, deflate=True, garbage=2)
    out_doc.close()
    clear_derotated_cache()
    print(f"  Saved: {output_path}")
    return anchors


def create_mcq_answer_strips(
    answers: dict[int, str],
    requested_questions: list[int],
) -> list[McqStrip]:
    """Return a single McqStrip listing MCQ answers as native PDF text."""
    found = [(q, answers[q]) for q in requested_questions if q in answers]
    if not found:
        return []
    # Estimate height: title row + gap + each answer row (~16pt)
    line_h = 16.0
    total_h = (
        _MCQ_TITLE_LINE_PT
        + _MCQ_AFTER_TITLE_GAP_PT
        + len(found) * line_h
        + 8.0
    )
    lines: list[tuple[str, bool]] = [("Multiple Choice Answers", True)]
    for qnum, letter in found:
        lines.append((f"Q{qnum}:  {letter}", False))
    return [McqStrip(lines=lines, display_h_pt=total_h)]
