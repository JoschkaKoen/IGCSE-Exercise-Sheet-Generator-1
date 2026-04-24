"""Pure geometry types and transforms for projecting scaffold bboxes onto scanned pages.

No PDF I/O — only scale+translation math and fitz.Rect construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import fitz  # PyMuPDF

from xscore.shared.models import BBox

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: y-coordinate (pt) that divides the 4-up page into top and bottom halves.
_RAW_MID_Y_PT: float = 420.9

#: Horizontal midpoint (pt) of the 4-up page — divides left from right sub-pages.
_RAW_MID_X_PT: float = 297.6

#: Step 10 projected overlay: trim this many PDF points from the left edge of every box.
_PROJECTED_TRIM_LEFT_PT: float = 22.0
#: Step 10: nudge boxes in the right column upward (PDF y decreases upward).
_PROJECTED_RIGHT_COLUMN_UP_PT: float = 2.0


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

class _Point(NamedTuple):
    x: float
    y: float


@dataclass
class SimilarityTransform:
    """Uniform-scale + translation transform from 4-up PDF points to scan pixels.

    Attributes:
        scale: Pixels per PDF point.
        tx:    X translation (px).
        ty:    Y translation (px).
    """
    scale: float
    tx: float
    ty: float

    def project_point(self, x_pt: float, y_pt: float) -> tuple[float, float]:
        """Map one point from 4-up PDF space to half-page pixel space."""
        return self.scale * x_pt + self.tx, self.scale * y_pt + self.ty

    def project_bbox(
        self, x0_pt: float, y0_pt: float, x1_pt: float, y1_pt: float
    ) -> tuple[float, float, float, float]:
        """Map a rectangle from 4-up PDF space to half-page pixel space."""
        x0, y0 = self.project_point(x0_pt, y0_pt)
        x1, y1 = self.project_point(x1_pt, y1_pt)
        return x0, y0, x1, y1

    def __str__(self) -> str:
        return f"scale={self.scale:.4f} px/pt  tx={self.tx:.1f}  ty={self.ty:.1f}"


def similarity_transform_to_dict(tf: SimilarityTransform) -> dict[str, float]:
    return {"scale": tf.scale, "tx": tf.tx, "ty": tf.ty}


def similarity_transform_from_dict(d: dict) -> SimilarityTransform:
    return SimilarityTransform(
        scale=float(d["scale"]),
        tx=float(d["tx"]),
        ty=float(d["ty"]),
    )


# ---------------------------------------------------------------------------
# Transform computation
# ---------------------------------------------------------------------------

def compute_half_transform(
    raw_left:  tuple[float, float],
    raw_right: tuple[float, float],
    scan_left:  tuple[float, float],
    scan_right: tuple[float, float],
) -> SimilarityTransform:
    """Compute a similarity transform from one pair of corresponding anchors.

    Args:
        raw_left:   ``(x, y)`` of the left IGCSE anchor in 4-up PDF pt.
        raw_right:  ``(x, y)`` of the right IGCSE anchor in 4-up PDF pt.
        scan_left:  ``(x, y)`` of the left IGCSE anchor in scan pixels
                    (half-page coordinates).
        scan_right: ``(x, y)`` of the right IGCSE anchor in scan pixels
                    (half-page coordinates).

    Returns:
        :class:`SimilarityTransform` mapping PDF pt → scan px.
    """
    dx_raw  = raw_right[0]  - raw_left[0]
    dx_scan = scan_right[0] - scan_left[0]
    scale = dx_scan / dx_raw
    tx = scan_left[0] - scale * raw_left[0]
    # Average the two y observations to reduce noise (both should map to same raw y)
    ty = (scan_left[1] + scan_right[1]) / 2.0 - scale * raw_left[1]
    return SimilarityTransform(scale=scale, tx=tx, ty=ty)


def compute_page_transforms(
    raw_anchors: dict[str, tuple[float, float]],
    scan_anchors: dict[str, dict | None],
) -> tuple[SimilarityTransform, SimilarityTransform]:
    """Return ``(top_transform, bot_transform)`` for one scanned page.

    Args:
        raw_anchors:  Output of :func:`extract_raw_igcse_anchors`.
        scan_anchors: The ``"anchors"`` sub-dict from one entry in the
                      anchor sidecar (``*_anchors.json`` or legacy ``*_reflines.json``).
                      Values are dicts with
                      ``"x"``, ``"y"``, ``"score"`` keys (or ``None``).

    Returns:
        ``(top_transform, bot_transform)`` as :class:`SimilarityTransform`.

    Raises:
        ValueError: If any required anchor is missing (``None``) in
            *scan_anchors*.
    """
    for key in ("top_left", "top_right", "bot_left", "bot_right"):
        if scan_anchors.get(key) is None:
            raise ValueError(
                f"[project_boxes_on_scanned_exam] Scan anchor '{key}' is missing "
                f"(template match failed for this page)."
            )

    def _scan(key: str) -> tuple[float, float]:
        a = scan_anchors[key]
        return float(a["x"]), float(a["y"])  # type: ignore[index]

    top_tf = compute_half_transform(
        raw_left=raw_anchors["top_left"],
        raw_right=raw_anchors["top_right"],
        scan_left=_scan("top_left"),
        scan_right=_scan("top_right"),
    )
    bot_tf = compute_half_transform(
        raw_left=raw_anchors["bot_left"],
        raw_right=raw_anchors["bot_right"],
        scan_left=_scan("bot_left"),
        scan_right=_scan("bot_right"),
    )
    return top_tf, bot_tf


# ---------------------------------------------------------------------------
# Bbox projection
# ---------------------------------------------------------------------------

def _adjust_raw_bbox_for_projected_overlay(
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    *,
    mid_x: float = _RAW_MID_X_PT,
) -> tuple[float, float, float, float]:
    """Tweaks in 4-up PDF space before similarity projection (step 10 overlay).

    - Every box: move the left edge right by :data:`_PROJECTED_TRIM_LEFT_PT` (trim
      from the left).
    - Right column (bbox center ``>= mid_x``): shift up by
      :data:`_PROJECTED_RIGHT_COLUMN_UP_PT` (PDF coordinates, y downward).
    """
    cx = (x0 + x1) / 2.0
    x0_adj = x0 + _PROJECTED_TRIM_LEFT_PT
    if x0_adj >= x1:
        x0_adj = x1 - 0.5
    if cx >= mid_x:
        y0_adj = y0 - _PROJECTED_RIGHT_COLUMN_UP_PT
        y1_adj = y1 - _PROJECTED_RIGHT_COLUMN_UP_PT
    else:
        y0_adj, y1_adj = y0, y1
    return x0_adj, y0_adj, x1, y1_adj


def _half_page_px_to_page_rect(
    x0_px: float,
    y0_px: float,
    x1_px: float,
    y1_px: float,
    half: str,
    mid_px: int,
    px_to_pt: float,
) -> fitz.Rect:
    """Map half-page pixel bbox to full-page PDF coordinates (points, top-left origin)."""
    xa, xb = sorted((x0_px, x1_px))
    ya, yb = sorted((y0_px, y1_px))
    y_off = 0 if half == "top" else float(mid_px)
    return fitz.Rect(
        xa * px_to_pt,
        (ya + y_off) * px_to_pt,
        xb * px_to_pt,
        (yb + y_off) * px_to_pt,
    )


def project_scaffold_bbox(
    bbox: BBox,
    top_transform: SimilarityTransform,
    bot_transform: SimilarityTransform,
    mid_y: float = _RAW_MID_Y_PT,
    mid_x: float = _RAW_MID_X_PT,
) -> tuple[float, float, float, float]:
    """Project one scaffold bbox from 4-up PDF space to half-page scan pixels.

    The bbox coordinate ``y0`` determines which transform is used:
    - ``y0 < mid_y``  → ``top_transform``; result is in the **top** half-page.
    - ``y0 >= mid_y`` → ``bot_transform``; result is in the **bottom** half-page.

    In both cases the returned coordinates are relative to the **top of that
    half** (y=0 = first row of the top or bottom half-page image).

    Before projection, raw PDF coordinates are adjusted for the step 10 overlay
    (left trim and a small upward nudge on the right column); see
    :func:`_adjust_raw_bbox_for_projected_overlay`.

    Args:
        bbox:          A :class:`shared.models.BBox` (or any object with
                       ``.x0``, ``.y0``, ``.x1``, ``.y1`` attributes), in
                       PDF points on the 4-up page.
        top_transform: Transform for bboxes whose ``y0`` is in the top half.
        bot_transform: Transform for bboxes whose ``y0`` is in the bottom half.
        mid_y:         The y-coordinate (pt) that divides top from bottom half
                       on the 4-up page.  Defaults to 420.9 pt.
        mid_x:         The x-coordinate (pt) that divides left from right
                       sub-page columns.  Defaults to 297.6 pt.

    Returns:
        ``(x0_px, y0_px, x1_px, y1_px)`` in half-page pixel coordinates
        (floats; caller should round/clip as needed for cropping).
    """
    tf = top_transform if bbox.y0 < mid_y else bot_transform
    x0, y0, x1, y1 = _adjust_raw_bbox_for_projected_overlay(
        float(bbox.x0),
        float(bbox.y0),
        float(bbox.x1),
        float(bbox.y1),
        mid_x=mid_x,
    )
    return tf.project_bbox(x0, y0, x1, y1)
