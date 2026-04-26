"""Skew-angle estimation: writing-line detection (preferred) + projection-variance fallback."""

from __future__ import annotations

import cv2
import numpy as np

from xscore.preprocessing.deskew.types import (
    _MIN_APPLY_DEG, _REFINE_MAX_ITERS,
    _SWEEP_COARSE_STEP, _SWEEP_FINE_HALF, _SWEEP_MAX, _SWEEP_MIN, _SWEEP_STEP,
    _WL_CLOSE_PX, _WL_MAX_HEIGHT_PX, _WL_MIN_LINES,
    _WL_MIN_PIXELS, _WL_MIN_WIDTH_FRAC, _WL_OPEN_FRAC, _WL_OPEN_MIN_PX,
)
from xscore.preprocessing.deskew.warp import _warp


def _best_angle_projection_variance(
    thresh: np.ndarray,
    angle_min: float,
    angle_max: float,
    angle_step: float,
) -> float:
    """Return angle in [*angle_min*, *angle_max*] that maximises column-sum variance."""
    h, w = thresh.shape[:2]
    cx, cy = w // 2, h // 2
    best_angle = 0.0
    best_var = -1.0
    for angle in np.arange(angle_min, angle_max + angle_step / 2, angle_step):
        M = cv2.getRotationMatrix2D((cx, cy), angle, 1.0)
        rotated = cv2.warpAffine(
            thresh, M, (w, h),
            flags=cv2.INTER_NEAREST,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        )
        proj = np.sum(rotated, axis=0, dtype=np.float64)
        v = float(np.var(proj))
        if v > best_var:
            best_var = v
            best_angle = float(angle)
    return best_angle


def get_deskew_angle(gray: np.ndarray) -> float:
    """Detect the skew angle of *gray* via vertical-projection variance.

    This is **not** printed vertical ruling-line detection — it only scores how
    vertical ink aligns at each trial angle to pick the deskew rotation.
    Ruling lines are found later by :func:`detect_reference_lines` on the
    **deskewed** half.

    Two-stage sweep on the same Otsu-thresholded full-resolution image:
    a **coarse** pass (0.1° steps) over ±3° to locate the maximum, then a
    **fine** pass (0.01° steps) within ±``_SWEEP_FINE_HALF``° of the coarse
    winner for 0.01° accuracy.  Using the same image for both stages ensures
    the fine window is always anchored in the correct region.

    Args:
        gray: Grayscale uint8 numpy array (any size).

    Returns:
        Best rotation angle in degrees.  Apply this angle directly to
        straighten the image (positive rotates CCW in OpenCV convention).
    """
    _, thresh_full = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
    )
    coarse_best = _best_angle_projection_variance(
        thresh_full, _SWEEP_MIN, _SWEEP_MAX, _SWEEP_COARSE_STEP
    )
    fine_lo = max(_SWEEP_MIN, coarse_best - _SWEEP_FINE_HALF)
    fine_hi = min(_SWEEP_MAX, coarse_best + _SWEEP_FINE_HALF)
    return _best_angle_projection_variance(
        thresh_full, fine_lo, fine_hi, _SWEEP_STEP
    )


def detect_writing_line_angle(gray: np.ndarray) -> tuple[float | None, int]:
    """Detect the median angle (deg from horizontal) of dotted writing lines.

    The exam pages have dotted horizontal lines that students write on.  These
    are physically printed on the paper, so they rotate exactly with the page —
    far more reliable than projection variance, which can be fooled by
    axis-aligned artifacts on pages with little text.

    Strategy:
      1. Otsu threshold (inverted: ink = 1).
      2. Horizontal morphological *closing* with a small kernel to bridge the
         gaps between dots, fusing each dotted line into a continuous segment.
      3. Horizontal *opening* with a long kernel to keep only writing-line-
         length structures (drops body text, tables, vertical strokes, etc.).
      4. Connected-component analysis: keep only blobs that span a large
         fraction of the page width and stay thin.
      5. ``cv2.fitLine`` on each blob's pixels → angle from horizontal.
      6. Median of valid angles.

    Returns ``(median_angle_deg, n_lines)``.  ``median_angle_deg`` is ``None``
    if no lines were detected; ``n_lines`` is the count.  Angle convention
    matches :func:`deskew_image` — apply ``+median`` to straighten the page.
    """
    h, w = gray.shape[:2]
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    close_k = cv2.getStructuringElement(cv2.MORPH_RECT, (_WL_CLOSE_PX, 1))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, close_k)

    open_w = max(_WL_OPEN_MIN_PX, int(_WL_OPEN_FRAC * w))
    open_k = cv2.getStructuringElement(cv2.MORPH_RECT, (open_w, 1))
    opened = cv2.morphologyEx(closed, cv2.MORPH_OPEN, open_k)

    n_lab, labels, stats, _ = cv2.connectedComponentsWithStats(opened, connectivity=8)
    angles: list[float] = []
    for k in range(1, n_lab):
        _x, _y, ww, hh, _area = stats[k]
        if ww < _WL_MIN_WIDTH_FRAC * w or hh > _WL_MAX_HEIGHT_PX:
            continue
        ys, xs = np.where(labels == k)
        if len(xs) < _WL_MIN_PIXELS:
            continue
        pts = np.column_stack([xs, ys]).astype(np.float32)
        vx, vy, _x0, _y0 = cv2.fitLine(pts, cv2.DIST_L2, 0, 0.01, 0.01).ravel()
        ang = float(np.degrees(np.arctan2(vy, vx)))
        # Normalize to (-90, 90]
        if ang > 90:
            ang -= 180
        elif ang <= -90:
            ang += 180
        angles.append(ang)
    if not angles:
        return None, 0
    return float(np.median(angles)), len(angles)


def iterative_deskew_angle(gray: np.ndarray) -> tuple[float, int, str]:
    """Refine the skew angle iteratively, preferring writing-line detection.

    Tries dotted-writing-line detection first (see
    :func:`detect_writing_line_angle`) — it directly measures the angle of
    physically-printed horizontal lines and is robust on pages where
    projection variance fails (e.g., minimal text content, JPEG block
    artifacts dominating the variance landscape).  Falls back to
    iterative projection variance via :func:`get_deskew_angle` when fewer
    than ``_WL_MIN_LINES`` writing lines are detected on the original
    image (e.g., cover/instruction pages with no answer area).

    Each iteration warps the *original* image by the running ``total``
    (never compounding warps — that introduces interpolation noise that
    destabilises convergence), measures residual, and accumulates.

    Returns ``(total_angle, iterations_consumed, method)`` where ``method``
    is ``"wl"`` (writing lines) or ``"proj"`` (projection variance fallback).
    """
    # First-pass writing-line check: only commit to writing-line iteration
    # if the original image yields enough detected lines.
    _, n_first = detect_writing_line_angle(gray)
    if n_first >= _WL_MIN_LINES:
        total = 0.0
        cur = gray
        for it in range(_REFINE_MAX_ITERS):
            a, n = detect_writing_line_angle(cur)
            if a is None or n < _WL_MIN_LINES:
                # Lost the line signal mid-iteration — keep the running total
                # we've already accumulated rather than discarding it.
                return total, it, "wl"
            if abs(a) < _MIN_APPLY_DEG:
                return total, it, "wl"
            total += a
            cur = _warp(gray, total)
        return total, _REFINE_MAX_ITERS, "wl"

    # Fallback: iterative projection variance.
    total = 0.0
    cur = gray
    for it in range(_REFINE_MAX_ITERS):
        a = get_deskew_angle(cur)
        if abs(a) < _MIN_APPLY_DEG:
            return total, it, "proj"
        total += a
        cur = _warp(gray, total)
    return total, _REFINE_MAX_ITERS, "proj"
