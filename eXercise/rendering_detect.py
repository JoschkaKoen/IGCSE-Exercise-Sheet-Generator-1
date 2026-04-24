"""QR code and barcode detection helpers for exam PDF strips."""

from __future__ import annotations

import fitz

from .config import QR_MARGIN_ZONE_PT, QR_MAX_SIZE_PT


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
