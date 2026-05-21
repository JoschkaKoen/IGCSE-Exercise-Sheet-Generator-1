"""Mark-scheme graphic extraction using PyMuPDF — no AI calls.

Detection strategy:
  1. Find raster images outside the page header zone (y > HEADER_PT).
     Table borders are vector drawings, not raster images, so this avoids
     false positives from the mark-scheme table structure.
  2. Compute the union bbox of those raster images, expand by SEARCH_MARGIN_PT
     to form a tight search clip.
  3. Pass that clip to _image_zone_clip() so it only pulls in drawings and
     text labels that are near the actual graphic — not the whole page's
     table structure.
  4. Crop from the original vector PDF at CROP_DPI and save.
"""
from __future__ import annotations
import re, time
from pathlib import Path
import fitz

OUT_DIR        = Path("/Users/joschka/Desktop/Programming/eXercise/scheme_graphics_test/pymupdf")
CROP_DPI       = 300
HEADER_PT      = 50.0   # ignore raster images whose top edge is above this y
SEARCH_MARGIN_PT = 20.0  # expand raster bbox when searching for nearby drawings

PDFS = [
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/s23 12/CS s23 12 Ex. all_answers.pdf"),
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/s23 22/CS s23 22 Ex. all_answers.pdf"),
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/w23 13/CS w23 13 Ex. all_answers.pdf"),
    Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/w23 23/CS w23 23 Ex. all_answers.pdf"),
]

# ---------------------------------------------------------------------------
# Copied verbatim from eXercise/mcq_image_extract.py
# ---------------------------------------------------------------------------
_IMAGE_ZONE_PAD_V_PT    = 5.0
_IMAGE_ZONE_PAD_H_PT    = 8.0
_OPTION_LABEL_RE        = re.compile(r"^\s*[A-D]\s*[.):]?\s*$")


def _is_substantial_drawing(r: fitz.Rect) -> bool:
    lo, hi = min(r.width, r.height), max(r.width, r.height)
    return lo >= 12.0 and hi >= 20.0


def _image_zone_clip(page: fitz.Page, question_clip: fitz.Rect) -> fitz.Rect:
    img_y0 = question_clip.y1
    img_y1 = question_clip.y0
    img_x0 = question_clip.x1
    img_x1 = question_clip.x0

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

    clip_drawings: list[tuple[fitz.Rect, float]] = []
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

    for r, half_w in clip_drawings:
        if (r.y1 + half_w) < img_y0 or (r.y0 - half_w) > img_y1:
            continue
        img_x0 = min(img_x0, r.x0 - half_w)
        img_x1 = max(img_x1, r.x1 + half_w)
        img_y0 = min(img_y0, r.y0 - half_w)
        img_y1 = max(img_y1, r.y1 + half_w)

    _LABEL_LOOK_ABOVE_PT   = 30.0
    _LABEL_LOOK_BELOW_PT   = 25.0
    _DIAGRAM_LABEL_MARGIN_PT = 15.0
    label_scan_top = max(question_clip.y0, img_y0 - _LABEL_LOOK_ABOVE_PT)
    label_scan_bot = min(question_clip.y1, img_y1 + _LABEL_LOOK_BELOW_PT)

    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        if block["bbox"][2] < question_clip.x0 or block["bbox"][0] > question_clip.x1:
            continue
        for line in block["lines"]:
            if not any(s["text"].strip() for s in line["spans"]):
                continue
            ly0, ly1   = line["bbox"][1], line["bbox"][3]
            lx0, lx1   = line["bbox"][0], line["bbox"][2]
            line_centre = (ly0 + ly1) / 2

            if (img_y0 - _DIAGRAM_LABEL_MARGIN_PT) <= line_centre <= (img_y1 + _DIAGRAM_LABEL_MARGIN_PT):
                img_y0 = min(img_y0, ly0);  img_y1 = max(img_y1, ly1)
                img_x0 = min(img_x0, lx0);  img_x1 = max(img_x1, lx1)
                continue

            if ly1 >= label_scan_top and ly0 <= label_scan_bot:
                line_text = "".join(s["text"] for s in line["spans"])
                if _OPTION_LABEL_RE.match(line_text):
                    img_y0 = min(img_y0, ly0);  img_y1 = max(img_y1, ly1)
                    img_x0 = min(img_x0, lx0);  img_x1 = max(img_x1, lx1)

    zone_y0 = max(question_clip.y0, img_y0 - _IMAGE_ZONE_PAD_V_PT)
    zone_y1 = min(question_clip.y1, img_y1 + _IMAGE_ZONE_PAD_V_PT)
    zone_x0 = max(question_clip.x0, img_x0 - _IMAGE_ZONE_PAD_H_PT)
    zone_x1 = min(question_clip.x1, img_x1 + _IMAGE_ZONE_PAD_H_PT)

    q_h = question_clip.height
    if q_h > 0 and (zone_y1 - zone_y0) / q_h > 0.9:
        return question_clip

    return fitz.Rect(zone_x0, zone_y0, zone_x1, zone_y1)


# ---------------------------------------------------------------------------
# Raster-image-only detection (avoids false positives from table borders)
# ---------------------------------------------------------------------------
def _raster_image_bbox(page: fitz.Page) -> fitz.Rect | None:
    """Return the union bbox of all raster images below the header zone, or None."""
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    found = False
    for img_item in page.get_images():
        xref = img_item[0]
        try:
            for rect in page.get_image_rects(xref):
                r = fitz.Rect(rect) & page.rect  # clip to visible area
                if r.is_empty or r.y0 < HEADER_PT:
                    continue
                x0 = min(x0, r.x0);  y0 = min(y0, r.y0)
                x1 = max(x1, r.x1);  y1 = max(y1, r.y1)
                found = True
        except Exception:
            continue
    if not found:
        return None
    bbox = fitz.Rect(x0, y0, x1, y1)
    # Reject if the union bbox itself is a tiny strip (e.g. a page-number badge)
    if bbox.height < 40.0:
        return None
    return bbox


def _vector_diagram_bbox(page: fitz.Page) -> fitz.Rect | None:
    """Return union bbox of compact interior drawings that form a vector diagram, or None."""
    pw = page.rect.width
    diag = []
    for d in page.get_drawings():
        r = fitz.Rect(d["rect"])
        if r.y0 < HEADER_PT:
            continue
        if not _is_substantial_drawing(r):
            continue
        # Skip margin decorations (small boxes at page edges)
        if r.x1 < 60 or r.x0 > pw - 70:
            continue
        # Skip table-structural rows (wide, near left border OR spanning >50% of page)
        if r.x0 < 100 and r.width > pw * 0.4:
            continue
        if r.width > pw * 0.5:
            continue
        # Skip grey-filled rectangles (table cells, decorative score boxes, code highlights)
        fill = d.get("fill")
        if fill is not None and 0.05 < sum(fill[:3]) / 3 < 0.99:
            continue
        diag.append(r)
    if len(diag) < 3:
        return None
    x0 = min(r.x0 for r in diag)
    y0 = min(r.y0 for r in diag)
    x1 = max(r.x1 for r in diag)
    y1 = max(r.y1 for r in diag)
    bbox = fitz.Rect(x0, y0, x1, y1)
    # If drawings span >45% of page height they're scattered table structure, not a diagram
    if bbox.height < 40.0 or bbox.height > page.rect.height * 0.45:
        return None
    return bbox


# ---------------------------------------------------------------------------
# Mark-scheme graphic zone (raster images + nearby drawings, no text expansion)
# ---------------------------------------------------------------------------
_ZONE_PAD_V_PT = 5.0
_ZONE_PAD_H_PT = 8.0

_SEPARATOR_MIN_WIDTH_PT  = 400.0  # wide black line = question separator
_SEPARATOR_MIN_HEIGHT_PT = 0.5   # thicker lines (h≈0.64) are question separators; thinner (h≈0.42) are row separators
_SEPARATOR_MAX_HEIGHT_PT = 1.5

def _find_separator_below(page: fitz.Page, y_ref: float) -> float | None:
    """Return y0 of the first full-width thick black separator line below y_ref, or None."""
    candidates = []
    for drawing in page.get_drawings():
        r = fitz.Rect(drawing["rect"])
        if r.y0 <= y_ref:
            continue
        if r.height < _SEPARATOR_MIN_HEIGHT_PT or r.height > _SEPARATOR_MAX_HEIGHT_PT:
            continue
        if r.width < _SEPARATOR_MIN_WIDTH_PT:
            continue
        fill = drawing.get("fill")
        if fill and all(c < 0.1 for c in fill):
            candidates.append(r.y0)
    return min(candidates) if candidates else None


def _scheme_graphic_zone(page: fitz.Page, raster_bbox: fitz.Rect) -> fitz.Rect:
    x0, y0, x1, y1 = raster_bbox.x0, raster_bbox.y0, raster_bbox.x1, raster_bbox.y1
    search = fitz.Rect(
        x0 - SEARCH_MARGIN_PT, y0 - SEARCH_MARGIN_PT,
        x1 + SEARCH_MARGIN_PT, y1 + SEARCH_MARGIN_PT,
    ) & page.rect

    for img_item in page.get_images():
        xref = img_item[0]
        try:
            for rect in page.get_image_rects(xref):
                r = fitz.Rect(rect)
                if not r.intersect(search).is_empty:
                    x0 = min(x0, r.x0); y0 = min(y0, r.y0)
                    x1 = max(x1, r.x1); y1 = max(y1, r.y1)
        except Exception:
            continue

    y0_floor = raster_bbox.y0 - SEARCH_MARGIN_PT
    for drawing in page.get_drawings():
        r = fitz.Rect(drawing["rect"])
        if r.intersect(search).is_empty:
            continue
        if not _is_substantial_drawing(r):
            continue
        # Skip table-structural rows: wide spans starting near the left border
        if r.x0 < 70 and r.width > page.rect.width * 0.4:
            continue
        half_w = (drawing.get("width") or 0) / 2
        x0 = min(x0, r.x0 - half_w)
        y0 = min(y0, max(r.y0 - half_w, y0_floor))
        x1 = max(x1, r.x1 + half_w)

    # Use separator line as hard y1 cutoff — search just below the last raster row
    sep_y = _find_separator_below(page, raster_bbox.y1 - SEARCH_MARGIN_PT)
    if sep_y is not None:
        y1 = min(y1, sep_y)

    # Add top padding only when drawings pulled y0 above the raster top (to avoid
    # including mark-scheme text that sits just above the raster).
    top_pad = _ZONE_PAD_V_PT if y0 < raster_bbox.y0 else 0.0

    zone = fitz.Rect(
        max(page.rect.x0, x0 - _ZONE_PAD_H_PT),
        max(page.rect.y0, y0 - top_pad),
        min(page.rect.x1, x1 + _ZONE_PAD_H_PT),
        min(page.rect.y1, y1 + 10.0),
    )
    return _trim_right(page, _snap_to_borders(page, zone))


def _trim_right(page: fitz.Page, zone: fitz.Rect) -> fitz.Rect:
    """Trim blank white columns from the right of zone using a low-res render."""
    pix = page.get_pixmap(dpi=72, clip=zone, colorspace=fitz.csGRAY)
    w, h = pix.width, pix.height
    if w == 0 or h == 0:
        return zone
    samples = pix.samples
    for col in range(w - 1, -1, -1):
        dark = sum(1 for row in range(h) if samples[row * w + col] < 230)
        if dark >= 3:
            new_x1 = zone.x0 + (col + 15) / w * zone.width
            return fitz.Rect(zone.x0, zone.y0, min(new_x1, zone.x1), zone.y1)
    return zone


def _is_dark(c) -> bool:
    return c is not None and all(v < 0.3 for v in c)


def _snap_to_borders(page: fitz.Page, zone: fitz.Rect) -> fitz.Rect:
    """Snap zone.x0 to a spanning left vertical border, zone.y1 to a spanning bottom horizontal border."""
    x0, y0, x1, y1 = zone.x0, zone.y0, zone.x1, zone.y1

    for d in page.get_drawings():
        r = fitz.Rect(d["rect"])
        c = d.get("fill") or d.get("color")
        if not _is_dark(c):
            continue

        # Left vertical border: thin, tall, inside zone near left edge
        if r.width < 3.0 and r.height > zone.height * 0.4:
            if zone.x0 <= r.x0 <= zone.x0 + 50:
                if r.y0 <= zone.y1 - zone.height * 0.3 and r.y1 >= zone.y0 + zone.height * 0.3:
                    x0 = max(x0, r.x0)

        # Bottom horizontal border: thin, wide, near or just below zone.y1
        if r.height < 3.0 and r.width > page.rect.width * 0.4:
            if zone.y1 - 20 <= r.y0 <= zone.y1 + 20:
                y1 = min(y1, r.y0)

    return fitz.Rect(x0, y0, x1, y1)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
OUT_DIR.mkdir(parents=True, exist_ok=True)

for pdf_path in PDFS:
    print(f"\n=== {pdf_path.name} ===")
    doc     = fitz.open(str(pdf_path))
    out_sub = OUT_DIR / pdf_path.stem
    out_sub.mkdir(exist_ok=True)

    for i in range(doc.page_count):
        t0       = time.perf_counter()
        page     = doc[i]
        page_num = i + 1

        raster_bbox = _raster_image_bbox(page)
        if raster_bbox is not None:
            zone = _scheme_graphic_zone(page, raster_bbox)
        else:
            vector_bbox = _vector_diagram_bbox(page)
            if vector_bbox is None:
                print(f"  p{page_num}: no graphic")
                continue
            zone = _scheme_graphic_zone(page, vector_bbox)
        pix      = page.get_pixmap(dpi=CROP_DPI, clip=zone)
        out_path = out_sub / f"p{page_num}.png"
        pix.save(str(out_path))
        elapsed  = round(time.perf_counter() - t0, 1)
        print(f"  p{page_num}: graphic found → {out_path.name}  ({pix.width}×{pix.height}px)  ({elapsed}s)")

print(f"\nSaved to: {OUT_DIR}")
