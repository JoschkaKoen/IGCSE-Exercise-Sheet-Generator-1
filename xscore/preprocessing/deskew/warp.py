"""Image rotation primitives for the deskew package."""

from __future__ import annotations

import cv2
import numpy as np

from xscore.preprocessing.deskew.types import _MIN_APPLY_DEG


def _warp(gray: np.ndarray, angle: float) -> np.ndarray:
    """Bicubic rotation with white border fill — applied unconditionally.

    Used by both the iterative probe loop (where we need the warp every time,
    even for tiny angles) and by :func:`deskew_image` (with a guard upstream).
    """
    h, w = gray.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(
        gray, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=255,
    )


def deskew_image(gray: np.ndarray, angle: float) -> np.ndarray:
    """Rotate *gray* by *angle* degrees (positive = CCW) at full resolution.

    Uses bicubic interpolation with white (255) border fill.
    Returns the original array unchanged if ``abs(angle) < _MIN_APPLY_DEG``.
    """
    if abs(angle) < _MIN_APPLY_DEG:
        return gray
    return _warp(gray, angle)
