"""IGCSE header template extraction + per-page template matching."""

from __future__ import annotations

import cv2
import numpy as np
import pytesseract
from PIL import Image

from xscore.preprocessing.deskew.types import (
    _ANCHOR_MIN_SCORE, _ANCHOR_SEARCH_HEIGHT, _ANCHOR_TEMPLATE_PADDING,
    AnchorPoint,
)


def extract_igcse_template(
    top_half_gray: np.ndarray,
    search_height: int = _ANCHOR_SEARCH_HEIGHT,
    padding: int = _ANCHOR_TEMPLATE_PADDING,
) -> np.ndarray:
    """Bootstrap the IGCSE label template from the left sub-page of *top_half_gray*.

    Runs Tesseract OCR on the top-left search strip of the **first** page's top
    half to find the word "IGCSE", then returns that region (with padding) as
    the template used for fast ``cv2.matchTemplate`` on all subsequent pages.

    Args:
        top_half_gray: Grayscale uint8 array for the top half of scan page 1.
        search_height: Number of rows to search from the top of the half.
        padding: Pixels to add around the detected OCR bounding box.

    Returns:
        Cropped grayscale template as a uint8 numpy array.

    Raises:
        RuntimeError: If "IGCSE" cannot be found in the expected region.
    """
    hh, hw = top_half_gray.shape[:2]
    mid_x = hw // 2
    # Search only the left sub-page header strip
    strip = top_half_gray[:min(search_height, hh), :mid_x]

    data = pytesseract.image_to_data(
        Image.fromarray(strip),
        output_type=pytesseract.Output.DICT,
    )

    best_conf = -1
    best_bbox: tuple[int, int, int, int] | None = None
    for i, text in enumerate(data["text"]):
        if "IGCSE" in text.upper():
            conf = int(data["conf"][i])
            if conf > best_conf:
                best_conf = conf
                best_bbox = (
                    int(data["left"][i]),
                    int(data["top"][i]),
                    int(data["left"][i]) + int(data["width"][i]),
                    int(data["top"][i]) + int(data["height"][i]),
                )

    if best_bbox is None:
        raise RuntimeError(
            "[deskew] Could not locate 'IGCSE' in the top-left header region of page 1. "
            "Ensure the scan is correctly oriented and the header is not obscured."
        )

    x0 = max(0, best_bbox[0] - padding)
    y0 = max(0, best_bbox[1] - padding)
    x1 = min(strip.shape[1], best_bbox[2] + padding)
    y1 = min(strip.shape[0], best_bbox[3] + padding)

    template = strip[y0:y1, x0:x1].copy()
    return template


def detect_igcse_anchors(
    half_gray: np.ndarray,
    template: np.ndarray,
    search_height: int = _ANCHOR_SEARCH_HEIGHT,
    min_score: float = _ANCHOR_MIN_SCORE,
) -> tuple[AnchorPoint | None, AnchorPoint | None]:
    """Locate the IGCSE header label on the left and right sub-pages of *half_gray*.

    Uses ``cv2.matchTemplate`` with ``TM_CCOEFF_NORMED`` inside a restricted
    search region — the top *search_height* rows of each left/right half —
    to avoid false positives from scattered "IGCSE" labels further down the page.

    All returned coordinates are in **half-page pixel space** (y=0 is the top of
    this half, not the top of the full A3 page).

    Args:
        half_gray: Grayscale uint8 array for one A4 half (top or bottom).
        template: Template cropped by ``extract_igcse_template``.
        search_height: Rows from the top of the half-page to restrict search.
        min_score: Minimum ``TM_CCOEFF_NORMED`` score; matches below this are
            discarded and ``None`` is returned for that side.

    Returns:
        ``(left_anchor, right_anchor)`` — either may be ``None`` if no confident
        match is found.
    """
    hh, hw = half_gray.shape[:2]
    mid_x = hw // 2
    th, tw = template.shape[:2]
    search_h = min(search_height, hh)

    def _match_in(region: np.ndarray, x_offset: int) -> AnchorPoint | None:
        if region.shape[0] < th or region.shape[1] < tw:
            return None
        result = cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < min_score:
            return None
        # max_loc is the top-left corner of the best-matching patch; anchor = center
        ax = x_offset + max_loc[0] + tw // 2
        ay = max_loc[1] + th // 2
        return AnchorPoint(x=int(ax), y=int(ay), score=round(float(max_val), 3))

    left_anchor  = _match_in(half_gray[:search_h, :mid_x],  x_offset=0)
    right_anchor = _match_in(half_gray[:search_h, mid_x:],  x_offset=mid_x)

    if left_anchor is None or right_anchor is None:
        from xscore.shared.terminal_ui import tool_line

        if left_anchor is None:
            tool_line("deskew", "WARNING: IGCSE anchor not found in left sub-page header")
        if right_anchor is None:
            tool_line("deskew", "WARNING: IGCSE anchor not found in right sub-page header")

    return left_anchor, right_anchor
