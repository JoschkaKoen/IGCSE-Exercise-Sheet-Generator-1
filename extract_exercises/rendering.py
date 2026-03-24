# -*- coding: utf-8 -*-
"""Vector PDF assembly: clip source pages with show_pdf_page, no rasterisation."""

from __future__ import annotations

from dataclasses import dataclass, field

import fitz

from .config import (
    A4_HEIGHT_PT,
    A4_WIDTH_PT,
    EXAM_LABEL_FONT_PT,
    HEADER_ZONE_MAX_Y_PT,
    MS_LANDSCAPE_H_THRESHOLD_PT,
    MS_LANDSCAPE_MARGIN_PT,
    MS_MARKS_START_PT,
    MS_PORTRAIT_MARKS_START_PT,
    MS_PORTRAIT_TABLE_LEFT_PT,
    MS_TABLE_LEFT_PT,
    QR_MARGIN_ZONE_PT,
    QR_MAX_SIZE_PT,
    STRIP_CROP_LEFT_PT,
    STRIP_CROP_RIGHT_PT,
    STRIP_CROP_TOP_PT,
)
from .mark_scheme import detect_landscape_ms_crop_x, detect_portrait_ms_crop_x

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

_MARGIN_PT = 15.0
_USABLE_W_PT = A4_WIDTH_PT - 2 * _MARGIN_PT   # 565 pt

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
            except Exception:
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
    except Exception:
        pass
    return rects


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
) -> list[Strip]:
    """Build a list of VectorStrip / GapStrip objects from (qnum, page_idx, y_start, y_end) tuples.

    All geometry is in PDF points; no rasterisation occurs here.
    """
    landscape_crop_x = MS_MARKS_START_PT
    portrait_crop_x = MS_PORTRAIT_MARKS_START_PT
    if is_ms:
        detected_l = detect_landscape_ms_crop_x(doc)
        if detected_l is not None:
            landscape_crop_x = detected_l
        detected_p = detect_portrait_ms_crop_x(doc)
        if detected_p is not None:
            portrait_crop_x = detected_p

    # Pre-collect QR rects per needed page
    needed_pages = set(r[1] for r in regions)
    qr_by_page: dict[int, list[fitz.Rect]] = {
        pi: collect_qr_image_rects(doc[pi]) for pi in needed_pages
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
            clip_x0 = MS_TABLE_LEFT_PT
            clip_x1 = landscape_crop_x
            clip_y0 = y_start
            clip_y1 = y_end
            content_w = A4_WIDTH_PT - 2 * MS_LANDSCAPE_MARGIN_PT
            display_w = content_w
            x_offset = MS_LANDSCAPE_MARGIN_PT
        elif is_ms:
            # Portrait mark-scheme page
            clip_x0 = MS_PORTRAIT_TABLE_LEFT_PT
            clip_x1 = portrait_crop_x
            clip_y0 = y_start
            clip_y1 = y_end
            clip_w = clip_x1 - clip_x0
            display_w = clip_w
            x_offset = (A4_WIDTH_PT - clip_w) / 2
        else:
            # Portrait question-paper page
            clip_x0 = STRIP_CROP_LEFT_PT
            clip_x1 = page_w - STRIP_CROP_RIGHT_PT
            clip_y0 = y_start
            clip_y1 = y_end
            # Shave the header zone top (QR / boilerplate band)
            if y_start <= HEADER_ZONE_MAX_Y_PT:
                clip_y0 = y_start + STRIP_CROP_TOP_PT
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

        # Separator between different questions
        if current_qnum is not None and qnum != current_qnum:
            strips.append(GapStrip(height_pt=8.0))

        strips.append(VectorStrip(
            src_doc=doc,
            page_idx=page_idx,
            clip_rect=clip_rect,
            display_h_pt=display_h,
            display_w_pt=display_w,
            x_offset_pt=x_offset,
            qr_rects=page_qrs,
        ))
        current_qnum = qnum

    return strips


# ---------------------------------------------------------------------------
# Header drawing
# ---------------------------------------------------------------------------

_HEADER_FS = float(EXAM_LABEL_FONT_PT)
_HEADER_H = _HEADER_FS + 8.0   # top-pad(4) + font(11) + bottom-pad(4) = 19 pt
_HEADER_BASELINE_Y = _HEADER_FS + 4.0  # baseline sits 4 pt below top of band


def _header_text(subject_label: str, paper_label: str | None) -> str:
    """Combine subject + paper into a single centred label."""
    if paper_label:
        return f"{subject_label}: {paper_label}"
    return subject_label


def _draw_header_line(out_page: fitz.Page, text: str) -> None:
    """Draw a single horizontally centred header line."""
    text_w = fitz.get_text_length(text, fontname="helv", fontsize=_HEADER_FS)
    x = (A4_WIDTH_PT - text_w) / 2
    out_page.insert_text(
        fitz.Point(x, _HEADER_BASELINE_Y),
        text,
        fontsize=_HEADER_FS,
        fontname="helv",
        color=(0.2, 0.2, 0.2),
        render_mode=0,
    )


def _erase_header_band(out_page: fitz.Page) -> None:
    """White-out the header band before redrawing with an updated label."""
    out_page.draw_rect(
        fitz.Rect(0, 0, A4_WIDTH_PT, _HEADER_H + 1),
        fill=(1, 1, 1), color=(1, 1, 1),
    )


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------

def layout_vector_strips_to_pdf(
    strips: list[Strip],
    output_path: str,
    header_label: str | None = None,
) -> None:
    """Flow strips onto A4 pages and write a vector PDF.

    Strips are VectorStrip (show_pdf_page), McqStrip (insert_text),
    GapStrip (whitespace), or str (paper sub-label).
    """
    hl = (header_label or "").strip() or None

    # Determine initial paper label (first str in strips, if any)
    current_paper_label: str | None = None
    for item in strips:
        if isinstance(item, str):
            current_paper_label = item
            break

    has_header = bool(hl or current_paper_label)
    header_h_pt = _HEADER_H if has_header else 0.0
    usable_h_pt = A4_HEIGHT_PT - 2 * _MARGIN_PT - header_h_pt
    initial_y_pt = _MARGIN_PT + header_h_pt

    out_doc = fitz.open()

    def new_page() -> tuple[fitz.Page, float]:
        pg = out_doc.new_page(width=A4_WIDTH_PT, height=A4_HEIGHT_PT)
        if has_header:
            _draw_header_line(pg, _header_text(hl or "", current_paper_label))
        return pg, initial_y_pt

    def redraw_header(pg: fitz.Page) -> None:
        _erase_header_band(pg)
        if has_header:
            _draw_header_line(pg, _header_text(hl or "", current_paper_label))

    current_page, y_cursor = new_page()

    for strip_idx, item in enumerate(strips):

        # --- paper sub-label (str) ---
        if isinstance(item, str):
            current_paper_label = item
            if y_cursor == initial_y_pt:
                # Still at top of page — just update the header in place
                redraw_header(current_page)
            else:
                # Anti-orphan: start a new page so the header matches its content
                current_page, y_cursor = new_page()
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
            for text, is_bold in item.lines:
                fs = bold_fs if is_bold else reg_fs
                current_page.insert_text(
                    fitz.Point(x_left, y_cursor + fs),
                    text,
                    fontsize=fs,
                    color=(0.0, 0.0, 0.0),
                    fontname="helv-b" if is_bold else "helv",
                )
                y_cursor += line_h if not is_bold else line_h + 2
            continue

        # --- vector content strip ---
        if isinstance(item, VectorStrip):
            sh = item.display_h_pt
            clip = item.clip_rect
            scale_x = item.display_w_pt / clip.width if clip.width > 0 else 1.0

            if y_cursor + sh > A4_HEIGHT_PT - _MARGIN_PT:
                if sh > usable_h_pt:
                    # Tall strip: chunk across pages
                    src_y0 = clip.y0
                    src_remaining = clip.height
                    while src_remaining > 0:
                        available_pt = A4_HEIGHT_PT - _MARGIN_PT - y_cursor
                        if available_pt < 5.0:
                            current_page, y_cursor = new_page()
                            available_pt = A4_HEIGHT_PT - _MARGIN_PT - y_cursor

                        src_chunk_h = min(src_remaining, available_pt / scale_x)
                        chunk_clip = fitz.Rect(
                            clip.x0, src_y0,
                            clip.x1, src_y0 + src_chunk_h,
                        )
                        out_h = src_chunk_h * scale_x
                        target = fitz.Rect(
                            item.x_offset_pt, y_cursor,
                            item.x_offset_pt + item.display_w_pt, y_cursor + out_h,
                        )
                        current_page.show_pdf_page(
                            target, item.src_doc, item.page_idx, clip=chunk_clip
                        )
                        for qr in item.qr_rects:
                            mapped = _map_source_to_output(qr, chunk_clip, target)
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

            target = fitz.Rect(
                item.x_offset_pt, y_cursor,
                item.x_offset_pt + item.display_w_pt, y_cursor + sh,
            )
            current_page.show_pdf_page(
                target, item.src_doc, item.page_idx, clip=clip
            )
            for qr in item.qr_rects:
                mapped = _map_source_to_output(qr, clip, target)
                if not mapped.is_empty:
                    current_page.draw_rect(mapped, fill=(1,1,1), color=(1,1,1))
            y_cursor += sh

    print(f"  Assembling {len(out_doc)} output page(s)...")
    out_doc.save(output_path, deflate=True, garbage=4)
    out_doc.close()
    print(f"  Saved: {output_path}")


def create_mcq_answer_strips(
    answers: dict[int, str],
    requested_questions: list[int],
) -> list[McqStrip]:
    """Return a single McqStrip listing MCQ answers as native PDF text."""
    found = [(q, answers[q]) for q in requested_questions if q in answers]
    if not found:
        return []
    # Estimate height: headline (~18pt) + each row (~16pt)
    line_h_bold = 18.0
    line_h = 16.0
    total_h = line_h_bold + len(found) * line_h + 8.0
    lines: list[tuple[str, bool]] = [("Multiple Choice Answers", True)]
    for qnum, letter in found:
        lines.append((f"Q{qnum}:  {letter}", False))
    return [McqStrip(lines=lines, display_h_pt=total_h)]
