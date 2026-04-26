"""Vertical reference-line detection on a deskewed A4 half-page."""

from __future__ import annotations

import cv2
import numpy as np

from xscore.preprocessing.deskew.types import (
    _ENDPOINT_MIN_RUN, _ENDPOINT_STRIP_HALF,
    _MAX_LINE_WIDTH, _MERGE_X_TOL, _MIN_LINE_HEIGHT_FRAC,
    _VKERNEL_HEIGHT, ReferenceLine,
)


def _scan_column_endpoints(
    binary: np.ndarray,
    x_center: int,
    strip_half: int = _ENDPOINT_STRIP_HALF,
    min_run: int = _ENDPOINT_MIN_RUN,
) -> tuple[int, int]:
    """Find the first and last ink row in a narrow column strip of *binary*.

    Extracts columns ``[x_center - strip_half .. x_center + strip_half]``,
    collapses them with ``np.max`` to a 1-D row mask.  To avoid single stray
    pixels or scan-edge artefacts (e.g. page border at y=0) being mistaken for
    the true line endpoint, requires ``min_run`` consecutive ink rows before
    declaring an endpoint valid.  This is immune to gaps in the *middle* of the
    line caused by handwriting or print breaks.

    Returns:
        (y_start, y_end) — both 0-indexed row numbers inclusive.
    """
    hh, hw = binary.shape[:2]
    x0 = max(0, x_center - strip_half)
    x1 = min(hw, x_center + strip_half + 1)
    strip = binary[:, x0:x1]
    row_mask = (np.max(strip, axis=1) > 0).astype(np.uint8)  # 0/1 per row

    # Find y_start: first row of a run >= min_run consecutive ink rows
    y_start = 0
    for r in range(hh - min_run + 1):
        if row_mask[r:r + min_run].all():
            y_start = r
            break

    # Find y_end: last row of a run >= min_run consecutive ink rows (scan backwards)
    y_end = hh - 1
    for r in range(hh - 1, min_run - 2, -1):
        if row_mask[r - min_run + 1:r + 1].all():
            y_end = r
            break

    return y_start, y_end


def detect_reference_lines(half_gray: np.ndarray) -> list[ReferenceLine]:
    """Locate the three vertical ruling lines on a deskewed A4 half-page.

    Two-step strategy:
    1. **x position** — morphological opening + connected-component analysis,
       which reliably isolates the long printed vertical structures and gives a
       stable ``x_center`` for each line.
    2. **y endpoints** — for each found x_center, scan a narrow column strip
       (±``_ENDPOINT_STRIP_HALF`` px) in the *original* binary image (before
       opening) for the first and last ink row.  This is immune to mid-line
       gaps caused by handwriting or printing breaks that would otherwise clip
       the blob's bounding box.

    Args:
        half_gray: Grayscale uint8 array for one A4 half (top or bottom).

    Returns:
        List of ``ReferenceLine`` objects sorted by x_center.
        Logs a warning to stdout if a count other than 3 is found.
    """
    hh, hw = half_gray.shape[:2]

    # Binarise once; reuse for both morphology and endpoint scan
    _, binary = cv2.threshold(half_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    # ---- Step 1: find x_center of each line via morphological opening --------
    vkernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, _VKERNEL_HEIGHT))
    v_mask = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vkernel, iterations=2)

    num_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        v_mask, connectivity=8
    )

    raw_x: list[int] = []
    for i in range(1, num_labels):
        x, _y, bw, bh, _area = stats[i]
        if bh > hh * _MIN_LINE_HEIGHT_FRAC and bw < _MAX_LINE_WIDTH:
            raw_x.append(int(x + bw // 2))

    # Merge x positions that belong to the same physical line
    raw_x.sort()
    merged_x: list[int] = []
    for xc in raw_x:
        if merged_x and abs(xc - merged_x[-1]) <= _MERGE_X_TOL:
            merged_x[-1] = (merged_x[-1] + xc) // 2
        else:
            merged_x.append(xc)

    # ---- Step 2: scan column strip for true y_start / y_end ------------------
    lines: list[ReferenceLine] = []
    for xc in merged_x:
        y_start, y_end = _scan_column_endpoints(binary, xc)
        lines.append(ReferenceLine(x_center=xc, y_start=y_start, y_end=y_end))

    if len(lines) != 3:
        from xscore.shared.terminal_ui import tool_line

        tool_line(
            "deskew",
            f"WARNING: expected 3 reference lines, found {len(lines)} (half size {hh}x{hw})",
        )

    return lines
