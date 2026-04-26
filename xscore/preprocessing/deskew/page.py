"""Per-page routing: A3 split into halves vs A4 whole-page deskew."""

from __future__ import annotations

import numpy as np
from PIL import Image

from xscore.preprocessing.deskew.angle import iterative_deskew_angle
from xscore.preprocessing.deskew.reflines import detect_reference_lines
from xscore.preprocessing.deskew.types import (
    _A3_HEIGHT_THRESHOLD_FACTOR, ReferenceLine,
)
from xscore.preprocessing.deskew.warp import deskew_image


_PageResult = tuple[
    Image.Image, float, float, list[ReferenceLine], list[ReferenceLine],
    int, int, str, str,
]


def deskew_page_halves(
    page_gray: np.ndarray,
) -> tuple[
    np.ndarray, float, float, list[ReferenceLine], list[ReferenceLine],
    int, int, str, str,
]:
    """Split *page_gray* at the vertical midpoint, deskew each half separately.

    After deskew, optionally runs :func:`detect_reference_lines` on each half
    (see ``config.DESKEW_DETECT_REFERENCE_LINES``; default off).

    Returns:
        (deskewed_full_page, top_angle, bot_angle, top_lines, bot_lines,
         top_iters, bot_iters, top_method, bot_method)
    """
    from xscore.config import DESKEW_DETECT_REFERENCE_LINES

    h = page_gray.shape[0]
    mid = h // 2

    top = page_gray[:mid, :]
    bot = page_gray[mid:, :]

    # 1) Estimate rotation per half (writing-line detection preferred,
    #    projection-variance fallback — see iterative_deskew_angle).
    top_angle, top_iters, top_method = iterative_deskew_angle(top)
    bot_angle, bot_iters, bot_method = iterative_deskew_angle(bot)

    # 2) Apply angular deskew before any morphological ruling-line detection.
    top_fixed = deskew_image(top, top_angle)
    bot_fixed = deskew_image(bot, bot_angle)

    # 3) Vertical ruling lines (optional; heavy morphology — off by default).
    if DESKEW_DETECT_REFERENCE_LINES:
        top_lines = detect_reference_lines(top_fixed)
        bot_lines = detect_reference_lines(bot_fixed)
    else:
        top_lines = []
        bot_lines = []

    return (np.vstack([top_fixed, bot_fixed]),
            top_angle, bot_angle, top_lines, bot_lines,
            top_iters, bot_iters, top_method, bot_method)


def deskew_whole_page(
    page_gray: np.ndarray,
) -> tuple[
    np.ndarray, float, float, list[ReferenceLine], list[ReferenceLine],
    int, int, str, str,
]:
    """Deskew *page_gray* as a single unit — A4 page mode.

    Unlike :func:`deskew_page_halves`, one angle is estimated over the full
    page and one rotation is applied.  ``bot_angle`` is always ``0.0``,
    ``bot_iters`` is always ``0``, ``bot_method`` is always ``""``, and both
    reference-line lists are empty.
    """
    angle, iters, method = iterative_deskew_angle(page_gray)
    fixed = deskew_image(page_gray, angle)
    return fixed, angle, 0.0, [], [], iters, 0, method, ""


def _process_page(
    args: tuple,
) -> tuple[
    int, Image.Image, float, float, list[ReferenceLine], list[ReferenceLine],
    int, int, str, str,
]:
    """Worker: deskew a single page and detect reference lines."""
    page_idx, pil_img, dpi = args
    gray = np.array(pil_img.convert("L"))
    if pil_img.height > _A3_HEIGHT_THRESHOLD_FACTOR * dpi:
        (fixed_gray, top_angle, bot_angle, top_lines, bot_lines,
         top_iters, bot_iters, top_method, bot_method) = deskew_page_halves(gray)
    else:
        (fixed_gray, top_angle, bot_angle, top_lines, bot_lines,
         top_iters, bot_iters, top_method, bot_method) = deskew_whole_page(gray)
    fixed_pil = Image.fromarray(fixed_gray, mode="L")
    return (page_idx, fixed_pil, top_angle, bot_angle,
            top_lines, bot_lines, top_iters, bot_iters, top_method, bot_method)
