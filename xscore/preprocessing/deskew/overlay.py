"""Visualization overlay: draw reference lines + IGCSE anchors on a PDF copy."""

from __future__ import annotations

import json
from pathlib import Path

import fitz  # PyMuPDF


# Default overlay stroke: vivid pink (RGB 0–1)
_OVERLAY_PINK: tuple[float, float, float] = (1.0, 0.35, 0.78)
_OVERLAY_LINE_WIDTH_PT = 0.35

# Anchor crosshair colour and arm length
_OVERLAY_ANCHOR_COLOR: tuple[float, float, float] = (1.0, 0.55, 0.0)  # orange
_OVERLAY_CROSSHAIR_SIZE_PT = 8.0  # pt — arm length of anchor crosshair markers


def overlay_reflines_on_pdf(
    deskewed_pdf: Path,
    reflines_json: Path,
    output_pdf: Path,
    dpi: int = 300,
    line_rgb: tuple[float, float, float] = _OVERLAY_PINK,
    line_width_pt: float = _OVERLAY_LINE_WIDTH_PT,
) -> Path:
    """Draw vertical reference lines from ``top`` / ``bot`` (pink, if present)
    and IGCSE anchor crosshairs on a **copy** of *deskewed_pdf*, saved to
    *output_pdf*.  When those arrays are empty, only anchors are drawn.

    Coordinates in the JSON are pixel offsets on each A4 **half** (top: y from
    page top; bottom: y from the half-page boundary). This matches
    ``deskew_page_halves`` / ``deskew_pdf_raster``.
    """
    deskewed_pdf = Path(deskewed_pdf)
    reflines_json = Path(reflines_json)
    output_pdf = Path(output_pdf)

    data: list[dict] = json.loads(reflines_json.read_text())
    px_to_pt = 72.0 / dpi

    from xscore.shared.terminal_ui import tool_line

    doc = fitz.open(str(deskewed_pdf))
    try:
        if len(data) != len(doc):
            tool_line(
                "reflines_overlay",
                f"WARNING: JSON has {len(data)} pages, PDF has {len(doc)} — overlaying min length",
            )

        for entry in data:
            idx = int(entry["page"]) - 1
            if idx < 0 or idx >= len(doc):
                continue
            page = doc[idx]
            h_px = int(round(page.rect.height / px_to_pt))
            mid = h_px // 2

            for ln in entry.get("top", []):
                xc = int(ln["x_center"])
                y0 = int(ln["y_start"])
                y1 = int(ln["y_end"])
                x_pt = xc * px_to_pt
                page.draw_line(
                    fitz.Point(x_pt, y0 * px_to_pt),
                    fitz.Point(x_pt, y1 * px_to_pt),
                    color=line_rgb,
                    width=line_width_pt,
                    lineCap=1,  # round caps
                )

            for ln in entry.get("bot", []):
                xc = int(ln["x_center"])
                y0 = mid + int(ln["y_start"])
                y1 = mid + int(ln["y_end"])
                x_pt = xc * px_to_pt
                page.draw_line(
                    fitz.Point(x_pt, y0 * px_to_pt),
                    fitz.Point(x_pt, y1 * px_to_pt),
                    color=line_rgb,
                    width=line_width_pt,
                    lineCap=1,
                )

            # Draw crosshair markers at each IGCSE anchor position
            anchors = entry.get("anchors", {})
            for key, half_offset_px in [
                ("top_left", 0), ("top_right", 0),
                ("bot_left", mid), ("bot_right", mid),
            ]:
                anc = anchors.get(key)
                if anc is None:
                    continue
                ax_pt = int(anc["x"]) * px_to_pt
                ay_pt = (half_offset_px + int(anc["y"])) * px_to_pt
                arm = _OVERLAY_CROSSHAIR_SIZE_PT
                page.draw_line(
                    fitz.Point(ax_pt - arm, ay_pt),
                    fitz.Point(ax_pt + arm, ay_pt),
                    color=_OVERLAY_ANCHOR_COLOR,
                    width=0.5,
                )
                page.draw_line(
                    fitz.Point(ax_pt, ay_pt - arm),
                    fitz.Point(ax_pt, ay_pt + arm),
                    color=_OVERLAY_ANCHOR_COLOR,
                    width=0.5,
                )

        doc.save(str(output_pdf), deflate=True)
    finally:
        doc.close()

    tool_line("reflines_overlay", "Saved.")
    return output_pdf
