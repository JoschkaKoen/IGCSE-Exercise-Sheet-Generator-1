"""Shared dataclasses + tuning constants for the deskew package.

Lives at the bottom of the import graph — every other module in
:mod:`xscore.preprocessing.deskew` may depend on it.
"""

from __future__ import annotations

from dataclasses import dataclass

from xscore.config import DESKEW_ACCURACY


# ---------------------------------------------------------------------------
# Angle detection / application
# ---------------------------------------------------------------------------

_SWEEP_MIN = -3.0           # deg
_SWEEP_MAX = 3.0            # deg
_SWEEP_STEP = 0.01          # deg — fine pass only (full-resolution thresh)
_SWEEP_COARSE_STEP = 0.1    # deg — coarse pass
_SWEEP_FINE_HALF = 0.15     # deg — fine window ± this around coarse best (covers grid error)
_MIN_APPLY_DEG = DESKEW_ACCURACY  # skip warp below this — matches fine-sweep resolution (default 0.01°)
_REFINE_MAX_ITERS = 5       # iterative-refinement cap (1–2 typical, 5 covers worst case)

# Writing-line (dotted answer-line) detection — primary deskew signal
_WL_CLOSE_PX = 15           # px — horizontal closing kernel; bridges gaps between dots
_WL_OPEN_FRAC = 0.08        # fraction of image width — opening kernel keeps only writing-line-length structures
_WL_OPEN_MIN_PX = 200       # px — floor on opening kernel width
_WL_MIN_WIDTH_FRAC = 0.30   # blob must span ≥ this fraction of image width
_WL_MAX_HEIGHT_PX = 30      # blob bounding-box height must be ≤ this (writing lines are thin)
_WL_MIN_PIXELS = 50         # blob must contain at least this many ink pixels (for fitLine reliability)
_WL_MIN_LINES = 3           # need at least this many detected lines for the angle to be trusted

# A3/A4 classifier: A4 portrait height ≈ 11.69×DPI, A3 portrait ≈ 16.54×DPI.
# Midpoint 14.1×DPI works at any configured DPI, not just 300.
_A3_HEIGHT_THRESHOLD_FACTOR = 14.1

# Morphological line detection
_VKERNEL_HEIGHT = 150       # px — minimum height a blob must survive MORPH_OPEN
_MIN_LINE_HEIGHT_FRAC = 0.3 # blob must span > this fraction of the half-page height
_MAX_LINE_WIDTH = 30        # px — blobs wider than this are not vertical lines
_MERGE_X_TOL = 10           # px — x-distance within which segments are merged
_ENDPOINT_STRIP_HALF = 4    # px — half-width of column strip used for endpoint scan
_ENDPOINT_MIN_RUN = 8       # px — minimum consecutive ink rows to count as a real endpoint

# IGCSE anchor detection
_ANCHOR_SEARCH_HEIGHT = 350  # px — rows from top of each half-page to search
_ANCHOR_MIN_SCORE = 0.5      # TM_CCOEFF_NORMED threshold for accepting a match
_ANCHOR_TEMPLATE_PADDING = 8 # px — padding around OCR bbox when cropping template


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ReferenceLine:
    """One detected vertical ruling line on a deskewed A4 half-page.

    All coordinates are in pixels relative to the top-left of the half-page
    image (y=0 is the top of that half, not the top of the full A3 page).
    """
    x_center: int   # horizontal centre of the line in pixels
    y_start: int    # topmost pixel of the detected blob
    y_end: int      # bottommost pixel of the detected blob

    def __str__(self) -> str:
        return f"x={self.x_center}  y={self.y_start}..{self.y_end}  h={self.y_end - self.y_start}"


@dataclass
class AnchorPoint:
    """Detected position of one IGCSE header label on a deskewed sub-page.

    Coordinates are in pixels relative to the top-left of the **half-page**
    image (i.e. y=0 is the top of that half, not the top of the full A3 page).
    """
    x: int          # center x of matched template in half-page pixel coords
    y: int          # center y of matched template in half-page pixel coords
    score: float    # template match confidence (TM_CCOEFF_NORMED, 0..1)

    def __str__(self) -> str:
        return f"({self.x},{self.y}) s={self.score:.2f}"
