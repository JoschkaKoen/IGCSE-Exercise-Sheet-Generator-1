# -*- coding: utf-8 -*-
"""Mark scheme detection, MCQ parsing, and answer-table regions."""

from __future__ import annotations

import re

from .config import (
    DEFAULT_SUBJECT_CONFIG,
    MS_LANDSCAPE_H_THRESHOLD_PT,
    SubjectConfig,
)


def _norm_bbox(page, bbox):
    """Transform a raw text bbox to the visual/display coordinate space.

    PyMuPDF's get_text("dict") returns coordinates in the *pre-rotation* page
    space, but rendered images (get_pixmap) and all crop logic use the *display*
    space.  For rotation=0 the two are identical.  For rotation=90 (CCW), which
    Cambridge used in 2023 mark schemes (portrait pages shown as landscape), the
    transform is:  x_d = mediabox.height − y_orig,  y_d = x_orig.
    """
    rot = page.rotation
    if rot == 0:
        return bbox
    x0, y0, x1, y1 = bbox
    if rot == 90:
        h = page.mediabox.height          # pre-rotation height (842 pt for s23)
        return (h - y1, x0, h - y0, x1)
    if rot == 270:
        w = page.mediabox.width
        return (y0, w - x1, y1, w - x0)
    if rot == 180:
        w, h = page.mediabox.width, page.mediabox.height
        return (w - x1, h - y1, w - x0, h - y0)
    return bbox


def detect_landscape_ms_crop_x(doc) -> float | None:
    """Auto-detect the x-coordinate to crop at for landscape MS pages.

    Finds the x-position of the Answer/Marks column separator by locating the
    rightmost wide-drawing x1 that lies strictly to the left of the 'Marks'
    column-header text.  Returns that x + 2 pt (to fully include the border line),
    or None if detection fails.
    """

    for pi in range(len(doc)):
        page = doc[pi]
        if page.rect.height >= MS_LANDSCAPE_H_THRESHOLD_PT:
            continue  # skip portrait pages
        text = page.get_text()
        if "Question" not in text or "Marks" not in text:
            continue
        marks_x = _find_marks_header_x(page)
        if marks_x is None:
            continue
        crop_x = _rightmost_drawing_x1_before(page, marks_x)
        if crop_x is not None:
            return crop_x
    return None


def detect_portrait_ms_crop_x(doc) -> float | None:
    """Auto-detect the x-coordinate to crop at for portrait MS pages.

    Same logic as ``detect_landscape_ms_crop_x`` but searches portrait pages.
    """

    for pi in range(len(doc)):
        page = doc[pi]
        if page.rect.height < MS_LANDSCAPE_H_THRESHOLD_PT:
            continue  # skip landscape pages
        text = page.get_text()
        if "Question" not in text or "Marks" not in text:
            continue
        marks_x = _find_marks_header_x(page)
        if marks_x is None:
            continue
        crop_x = _rightmost_drawing_x1_before(page, marks_x)
        if crop_x is not None:
            return crop_x
    return None


def _find_marks_header_x(page) -> float | None:
    """Return the display-space x0 of the 'Marks' column header on *page*, or None."""
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            t = "".join(s["text"] for s in line["spans"]).strip()
            if t != "Marks":
                continue
            nx0, ny0, _, _ = _norm_bbox(page, line["bbox"])
            if ny0 < 100:  # only the column header near the top
                return nx0
    return None


def _rightmost_drawing_x1_before(page, marks_x: float) -> float | None:
    """Return the largest drawing-rect x1 that is strictly less than *marks_x*.

    First finds the rightmost edge of wide drawings (≥30 pt, cell fills /
    horizontal borders).  Then extends to any narrow vertical border line whose
    x0 is within 1 pt of that edge — this catches the right border stroke
    without jumping across to the Marks column's left border.
    """
    best = None
    for r in page.get_drawings():
        dr = _norm_bbox(page, (r["rect"].x0, r["rect"].y0, r["rect"].x1, r["rect"].y1))
        if dr[2] - dr[0] < 30:
            continue
        if dr[2] < marks_x:
            if best is None or dr[2] > best:
                best = dr[2]
    if best is None:
        return None
    # Extend to adjacent narrow vertical border lines (within 1 pt of the wide edge).
    for r in page.get_drawings():
        dr = _norm_bbox(page, (r["rect"].x0, r["rect"].y0, r["rect"].x1, r["rect"].y1))
        w = dr[2] - dr[0]
        h = dr[3] - dr[1]
        if w < 5 and h >= 3 and dr[2] < marks_x:
            if dr[0] < best - 0.1 and dr[2] > best:
                best = dr[2]
    return best


def _leftmost_drawing_x0(page, marks_x: float) -> float | None:
    """Return the smallest drawing-rect x0 to the left of *marks_x*.

    Considers wide drawings (≥30 pt) AND tall narrow drawings (height ≥10 pt,
    i.e. vertical border lines) so that table border strokes are included.
    """
    best = None
    for r in page.get_drawings():
        dr = _norm_bbox(page, (r["rect"].x0, r["rect"].y0, r["rect"].x1, r["rect"].y1))
        w = dr[2] - dr[0]
        h = dr[3] - dr[1]
        if w < 30 and h < 3:
            continue
        if dr[2] < marks_x:
            if best is None or dr[0] < best:
                best = dr[0]
    return best


def detect_landscape_ms_table_left(doc) -> float | None:
    """Auto-detect the leftmost x of table content on landscape MS pages.

    Returns the x0 of the leftmost wide drawing that is part of the answer
    table (to the left of the Marks column), minus a small padding.
    """

    for pi in range(len(doc)):
        page = doc[pi]
        if page.rect.height >= MS_LANDSCAPE_H_THRESHOLD_PT:
            continue
        text = page.get_text()
        if "Question" not in text or "Marks" not in text:
            continue
        marks_x = _find_marks_header_x(page)
        if marks_x is None:
            continue
        left_x = _leftmost_drawing_x0(page, marks_x)
        if left_x is not None:
            return left_x
    return None


def detect_portrait_ms_table_left(doc) -> float | None:
    """Auto-detect the leftmost x of table content on portrait MS pages."""

    for pi in range(len(doc)):
        page = doc[pi]
        if page.rect.height < MS_LANDSCAPE_H_THRESHOLD_PT:
            continue
        text = page.get_text()
        if "Question" not in text or "Marks" not in text:
            continue
        marks_x = _find_marks_header_x(page)
        if marks_x is None:
            continue
        left_x = _leftmost_drawing_x0(page, marks_x)
        if left_x is not None:
            return left_x
    return None


def detect_ms_type(doc):
    """Detect whether a mark scheme is MCQ or structured."""
    text = doc[0].get_text()
    if "Multiple Choice" in text:
        return "mcq"
    return "structured"


def parse_mcq_answers(doc):
    """Parse MCQ mark scheme: returns dict {question_number: answer_letter}."""
    answers = {}
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        blocks = page.get_text("dict")["blocks"]
        rows = {}
        for block in blocks:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                if not line["spans"]:
                    continue
                y = round(line["bbox"][1], 0)
                x = line["bbox"][0]
                text = "".join(s["text"] for s in line["spans"]).strip()
                if text:
                    if y not in rows:
                        rows[y] = []
                    rows[y].append((x, text))

        for y in sorted(rows.keys()):
            items = sorted(rows[y], key=lambda t: t[0])
            if len(items) >= 2:
                qtext = items[0][1]
                atext = items[1][1]
                if re.match(r"^\d{1,2}$", qtext) and re.match(r"^[A-D]$", atext):
                    answers[int(qtext)] = atext
    return answers


def find_ms_answer_pages(doc, cfg: SubjectConfig | None = None):
    """Find pages in the mark scheme that contain the actual answer tables."""
    cfg = cfg or DEFAULT_SUBJECT_CONFIG
    answer_pages = []
    for pi in range(len(doc)):
        page = doc[pi]
        text = page.get_text()
        if "Question" not in text or cfg.ms_marks_column_keyword not in text:
            continue
        blocks = page.get_text("dict")["blocks"]
        for b in blocks:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                line_text = "".join(s["text"] for s in line["spans"]).strip()
                nx0, ny0, _, _ = _norm_bbox(page, line["bbox"])
                if re.match(r"^\d{1,2}\(", line_text) or (
                    re.match(r"^\d{1,2}$", line_text) and nx0 < 110 and ny0 > 60
                ):
                    if nx0 < 110:
                        if pi not in answer_pages:
                            answer_pages.append(pi)
                        break
    return answer_pages


def _collect_header_rows(doc, answer_pages):
    """Return a dict {page_index: [(y_top, y_bottom), ...]} for every
    'Question / Answer / Marks' header row found on each answer page (works for
    both landscape and portrait mark schemes).

    Cambridge IGCSE mark schemes repeat this header row not only at the top of each
    page but also between question groups within a page.  These repeated headers must
    be excluded from answer strips.
    """
    result = {}
    for pi in answer_pages:
        page = doc[pi]
        rows = []
        for b in page.get_text("dict")["blocks"]:
            if b["type"] != 0:
                continue
            for line in b["lines"]:
                text = "".join(s["text"] for s in line["spans"]).strip()
                nx0, ny0, nx1, ny1 = _norm_bbox(page, line["bbox"])
                if nx0 > 120:
                    continue
                # Top-of-page and mid-page repeated column headers; some PDFs split spans.
                if text == "Question" or (
                    "Question" in text and ("Marks" in text or "Answer" in text)
                ):
                    rows.append((ny0, ny1))
        result[pi] = sorted(rows, key=lambda h: h[0])
    return result


def _cap_y_end_before_headers(y_start, y_end, header_rows_for_page, page=None):
    """Return y_end capped just before the first header row that lies inside
    (y_start, y_end).  The top-of-page header (y_start is already set to skip
    it) is never a problem; only mid-page repeated headers matter.

    When *page* is supplied, also scans for the wide horizontal line that forms
    the header's top cell border (a drawing, not text).  That border typically
    sits 3–8 pt above the 'Question' text and must be excluded from the strip,
    otherwise it appears as an orphaned line below the last answer row.
    """
    for h_top, _h_bot in header_rows_for_page:
        if y_start < h_top < y_end:
            cap = h_top - 2
            if page is not None:
                for d in page.get_drawings():
                    r = d["rect"]
                    dr = _norm_bbox(page, (r.x0, r.y0, r.x1, r.y1))
                    if dr[2] - dr[0] < 50:          # skip narrow elements
                        continue
                    # Wide drawing whose bottom sits just above the header text
                    if dr[3] <= h_top and dr[3] > h_top - 15:
                        cap = min(cap, dr[1])
            return cap
    return y_end


def _precise_y_start_from_drawings(page, y_start: float, h_top: float, h_bot: float,
                                    first_entry_y: float) -> float:
    """Refine y_start using drawing geometry so we begin at the first content-row border.

    1.  Find the extent of the header band: the highest y1 of any wide drawing that
        *starts* within 15 pt of h_top and *ends* after h_bot but before first_entry_y.
    2.  Find the first wide drawing that starts *after* that header-band extent — that
        drawing is the top border of the first content row.  Use its y0 as y_start.

    Falls back gracefully when no drawings are found (returns the incoming y_start).
    """
    header_band_end = h_bot
    for d in page.get_drawings():
        r = d["rect"]
        dr = _norm_bbox(page, (r.x0, r.y0, r.x1, r.y1))
        if dr[2] - dr[0] < 50:
            continue
        # Only thick drawings (filled header background, ≥2 pt tall) count as
        # header band; thin border lines (<1 pt) should not push the extent.
        if dr[3] - dr[1] < 2.0:
            continue
        if dr[1] >= h_top - 15 and dr[1] > h_bot and dr[1] < first_entry_y:
            header_band_end = max(header_band_end, dr[1])

    content_row_top = None
    for d in page.get_drawings():
        r = d["rect"]
        dr = _norm_bbox(page, (r.x0, r.y0, r.x1, r.y1))
        if dr[2] - dr[0] < 50:
            continue
        if dr[1] > header_band_end and dr[1] < first_entry_y:
            if content_row_top is None or dr[1] < content_row_top:
                content_row_top = dr[1]

    # Include a margin above the border so the top stroke is not clipped.
    # Use 2 pt to ensure the full border line (typically 0.48 pt thick)
    # plus any anti-aliasing is captured.  Clamp so we never go above
    # header_band_end (avoids capturing header-band artefacts on portrait pages).
    if content_row_top is not None:
        return max(y_start, header_band_end, content_row_top - 2.0)
    return max(y_start, header_band_end)


def _snap_y_start_to_cell_border(page, y_start: float, first_entry_y: float) -> float:
    """Snap y_start to the top border of the cell containing first_entry_y.

    Scans for wide horizontal drawings (table cell borders) in the zone between
    y_start and first_entry_y.  The closest border *above* first_entry_y is
    the top edge of this question's row.  We start exactly at its y0 so the
    border stroke is included but nothing from the cell/header above leaks in.

    Falls back to the incoming y_start when no suitable border is found.
    """
    best_border_y0 = None
    for d in page.get_drawings():
        r = d["rect"]
        dr = _norm_bbox(page, (r.x0, r.y0, r.x1, r.y1))
        draw_w = dr[2] - dr[0]
        draw_h = dr[3] - dr[1]
        if draw_w < 50:
            continue
        # We want thin horizontal lines (cell borders, typically <2 pt tall)
        # whose vertical midpoint sits between y_start and first_entry_y.
        mid_y = (dr[1] + dr[3]) / 2
        if mid_y < y_start or mid_y > first_entry_y:
            continue
        if draw_h > 5:
            continue  # skip thick fills / header bands
        if best_border_y0 is None or dr[1] > best_border_y0:
            best_border_y0 = dr[1]
    if best_border_y0 is not None:
        return best_border_y0
    return y_start


def _floor_y_start_below_headers(first_line_y, candidate_y_start, header_rows_for_page,
                                  separator_below_header_pt=5.65):
    """Raise ``y_start`` so the strip begins *below* any table header row that sits
    above the question's first line.

    Without this, the next question's region can start at ``first_line - 10pt`` and
    still include one scan line of the repeated 'Question / Answer / Marks' row that
    sits between questions (e.g. between Q7 and Q8).

    ``separator_below_header_pt`` is the gap between the header text's bottom bbox
    and the top cell-border of the next data row:
    - Landscape MS: 5.65 pt (thick gray separator below header text, 5.64 pt tall)
    - Portrait MS: ~3.0 pt (thin separator; calibrated from drawing y-coordinates)
    """
    y = candidate_y_start
    for h_top, h_bot in header_rows_for_page:
        if h_bot < first_line_y:
            y = max(y, h_bot + separator_below_header_pt)
    return y


def _tight_y_end(page, y_start, y_end_max, trailing_gap_pt: float,
                 cfg: SubjectConfig | None = None) -> float:
    """Return the bottom of all visible content on *page* inside (y_start, y_end_max).

    Scans both text lines and drawn elements (table cell borders are drawn paths,
    not text).  Whichever is furthest down determines the cut point:

    * If a drawing (width ≥ cfg.drawing_min_width_pt) is the bottommost element,
      return drawing_y + cfg.drawing_bottom_pad_pt (the border itself is the last
      thing; minimal padding ensures the border pixel is fully included without
      pulling in the next row's top whitespace).
    * Otherwise return last_text_y + ``trailing_gap_pt`` so closing cell borders
      drawn ~20–30 pt below the last text row are still captured.

    ``trailing_gap_pt`` should be cfg.trailing_gap_capped_pt when a header-cap has
    already been applied and cfg.trailing_gap_uncapped_pt when no cap was active.
    """
    cfg = cfg or DEFAULT_SUBJECT_CONFIG
    min_draw_w = cfg.drawing_min_width_pt

    last_text_y = None
    for b in page.get_text("dict")["blocks"]:
        if b["type"] != 0:
            continue
        for line in b["lines"]:
            nx0, ny0, nx1, ny1 = _norm_bbox(page, line["bbox"])
            if ny0 <= y_start or ny1 >= y_end_max:
                continue
            if nx0 < 55 or nx0 > 810:
                continue
            t = "".join(s["text"] for s in line["spans"]).strip()
            if t and (last_text_y is None or ny1 > last_text_y):
                last_text_y = ny1

    # Extend to the bottom of wide drawn elements (horizontal table borders).
    # Only consider drawings whose left edge (x0) is in the Question/Answer
    # column area (x0 < 300 pt).  Formula elements (fraction bars, etc.) in
    # the Marks/Partial-Marks columns can be wider than min_draw_w and would
    # otherwise push the crop lower than the actual table border.
    last_drawing_y = None
    for d in page.get_drawings():
        r = d["rect"]
        dr = _norm_bbox(page, (r.x0, r.y0, r.x1, r.y1))
        if dr[2] - dr[0] < min_draw_w:
            continue
        if dr[0] > 300:
            continue
        if dr[3] <= y_start or dr[3] > y_end_max:
            continue
        if last_drawing_y is None or dr[3] > last_drawing_y:
            last_drawing_y = dr[3]

    if last_text_y is None and last_drawing_y is None:
        return y_end_max

    # Prefer the drawing (table border) whenever one exists at or below the
    # last text row.  The old threshold (drawing > text + 5) missed borders that
    # sit just 2–4 pt below the last text, falling through to the text path
    # which added 20–32 pt of trailing space — causing irregular gaps and
    # visible whitespace below the bottom border line.
    if last_drawing_y is not None and (
        last_text_y is None or last_drawing_y >= last_text_y - 2
    ):
        return last_drawing_y + cfg.drawing_bottom_pad_pt
    return (last_text_y or 0) + trailing_gap_pt


def find_ms_answer_regions(doc, requested_questions, cfg: SubjectConfig | None = None):
    """Find answer regions in a structured mark scheme."""
    cfg = cfg or DEFAULT_SUBJECT_CONFIG
    answer_pages = find_ms_answer_pages(doc, cfg)

    if not answer_pages:
        print("  Warning: No answer table pages found in mark scheme.")
        return []

    # Pre-collect repeated header row positions so we can exclude them from strips.
    page_header_rows = _collect_header_rows(doc, answer_pages)

    all_entries = []

    for pi in answer_pages:
        page = doc[pi]
        page_height = page.rect.height
        blocks = page.get_text("dict")["blocks"]

        for block in blocks:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                if not line["spans"]:
                    continue
                first_span = line["spans"][0]
                text = first_span["text"].strip()

                # Normalise to display (visual) coordinates so that the same
                # filters work for both native-landscape (s25, rotation=0) and
                # rotated-portrait pages (s23, rotation=90).
                nx0, ny0, _, _ = _norm_bbox(page, line["bbox"])

                if ny0 < 50 or ny0 > page_height - 30:
                    continue
                if nx0 > 110:
                    continue

                m = re.match(r"^(\d{1,2})(\(|$)", text)
                if m and text != "Question":
                    top_q = int(m.group(1))
                    if 1 <= top_q <= 40:
                        all_entries.append((top_q, pi, ny0, text))

    if not all_entries:
        print("  Warning: No question entries found in mark scheme tables.")
        return []

    all_entries.sort(key=lambda x: (x[1], x[2]))

    regions = []
    for qnum in requested_questions:
        q_entries = [e for e in all_entries if e[0] == qnum]
        if not q_entries:
            print(f"  Warning: No mark scheme entry for Q{qnum}")
            continue

        first_entry = q_entries[0]
        last_entry = q_entries[-1]
        last_idx = all_entries.index(last_entry)

        if last_idx + 1 < len(all_entries):
            next_entry = all_entries[last_idx + 1]
            if next_entry[1] == last_entry[1]:
                y_end = next_entry[2] + 1.0
            else:
                y_end = doc[last_entry[1]].rect.height - 30
        else:
            y_end = doc[last_entry[1]].rect.height - 30

        first_page = first_entry[1]
        last_page = last_entry[1]
        is_landscape_page = doc[first_page].rect.height < MS_LANDSCAPE_H_THRESHOLD_PT
        _sep = 5.65 if is_landscape_page else 3.0
        y_start = max(cfg.ms_header_bottom_pt, first_entry[2] - 10)
        _first_page_hdrs = page_header_rows.get(first_page, [])
        y_start = _floor_y_start_below_headers(
            first_entry[2],
            y_start,
            _first_page_hdrs,
            separator_below_header_pt=_sep,
        )
        # Refine using drawing geometry: skips past the full header-band drawing and
        # anchors y_start to the top border of the first content row.
        if _first_page_hdrs:
            h_top_fp, h_bot_fp = _first_page_hdrs[0]
            y_start = _precise_y_start_from_drawings(
                doc[first_page], y_start, h_top_fp, h_bot_fp, first_entry[2]
            )
        # Snap to the nearest cell border above the question text so we never
        # include content from the cell above.
        y_start = _snap_y_start_to_cell_border(doc[first_page], y_start, first_entry[2])

        def _y_end_cap(page):
            return cfg.ms_footer_top_pt if page.rect.height < MS_LANDSCAPE_H_THRESHOLD_PT else page.rect.height - 50

        def _mid_y_start(page):
            # Always skip at least the table header band; portrait mids used y=50 before
            # and re-included repeated column headers.
            return cfg.ms_header_bottom_pt

        y_end = min(y_end, _y_end_cap(doc[last_page]))

        if first_page == last_page:
            # For single-page questions y_start is the correct lower bound.
            _y_end_raw = y_end
            y_end = _cap_y_end_before_headers(
                y_start, y_end, page_header_rows.get(last_page, []), doc[last_page]
            )
            _tight_gap = cfg.trailing_gap_capped_pt if y_end < _y_end_raw else cfg.trailing_gap_uncapped_pt
            y_end = min(y_end, _tight_y_end(doc[first_page], y_start, y_end, _tight_gap, cfg))
            regions.append((qnum, first_page, y_start, y_end))
        else:
            first_y_end = min(doc[first_page].rect.height - 30, _y_end_cap(doc[first_page]))
            # Cap first-page y_end if a repeated column-header row appears below the
            # question's first entry (prevents including the next section's header).
            _first_y_end_raw = first_y_end
            first_y_end = _cap_y_end_before_headers(
                y_start, first_y_end, page_header_rows.get(first_page, []), doc[first_page]
            )
            _tight_gap_first = cfg.trailing_gap_capped_pt if first_y_end < _first_y_end_raw else cfg.trailing_gap_uncapped_pt
            first_y_end = min(first_y_end, _tight_y_end(doc[first_page], y_start, first_y_end, _tight_gap_first, cfg))
            regions.append((qnum, first_page, y_start, first_y_end))
            for mid_p in range(first_page + 1, last_page):
                if mid_p in answer_pages:
                    mid_ys = _mid_y_start(doc[mid_p])
                    on_mid = [e for e in q_entries if e[1] == mid_p]
                    if on_mid:
                        first_y_mid = min(e[2] for e in on_mid)
                        _mid_sep = 5.65 if doc[mid_p].rect.height < MS_LANDSCAPE_H_THRESHOLD_PT else 3.0
                        mid_ys = _floor_y_start_below_headers(
                            first_y_mid,
                            mid_ys,
                            page_header_rows.get(mid_p, []),
                            separator_below_header_pt=_mid_sep,
                        )
                    if on_mid:
                        mid_ys = _snap_y_start_to_cell_border(doc[mid_p], mid_ys, first_y_mid)
                    mid_ye = min(doc[mid_p].rect.height - 30, _y_end_cap(doc[mid_p]))
                    mid_ye = _cap_y_end_before_headers(
                        mid_ys, mid_ye, page_header_rows.get(mid_p, []), doc[mid_p]
                    )
                    regions.append((qnum, mid_p, mid_ys, mid_ye))
            on_last = [e for e in q_entries if e[1] == last_page]
            first_on_last = min(e[2] for e in on_last)
            last_ys = _mid_y_start(doc[last_page])
            _last_sep = 5.65 if doc[last_page].rect.height < MS_LANDSCAPE_H_THRESHOLD_PT else 3.0
            _last_hdrs = page_header_rows.get(last_page, [])
            last_ys = _floor_y_start_below_headers(
                first_on_last,
                last_ys,
                _last_hdrs,
                separator_below_header_pt=_last_sep,
            )
            if _last_hdrs:
                lh_top, lh_bot = _last_hdrs[0]
                last_ys = _precise_y_start_from_drawings(
                    doc[last_page], last_ys, lh_top, lh_bot, first_on_last
                )
            last_ys = _snap_y_start_to_cell_border(doc[last_page], last_ys, first_on_last)
            _y_end_raw_last = y_end
            y_end = _cap_y_end_before_headers(
                last_ys, y_end, page_header_rows.get(last_page, []), doc[last_page]
            )
            _tight_gap_last = cfg.trailing_gap_capped_pt if y_end < _y_end_raw_last else cfg.trailing_gap_uncapped_pt
            y_end = min(y_end, _tight_y_end(doc[last_page], last_ys, y_end, _tight_gap_last, cfg))
            regions.append((qnum, last_page, last_ys, y_end))

    return regions
