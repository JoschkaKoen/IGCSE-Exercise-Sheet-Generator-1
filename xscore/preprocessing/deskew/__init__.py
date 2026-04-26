"""Automated fine deskew for scanned A3-portrait exam papers.

Each A3 page contains two A4 exam sheets (top half / bottom half). The scanner
introduces independent sub-degree skew in each half, so angle detection and
correction are performed **per half** and the halves are reassembled.

The sidecar ``<stem>_anchors.json`` (next to the output PDF by default) stores
**vertical ruling lines** per half (``top`` / ``bot``) from
:func:`detect_reference_lines` when ``config.DESKEW_DETECT_REFERENCE_LINES`` is
true (default false). **IGCSE header anchors** are written as ``null`` by
:func:`deskew_pdf_raster` and filled later by
:func:`detect_page_anchors_for_cleaned_scan` (pipeline step 8).

Public symbols are re-exported from the focused submodules; importing
``from xscore.preprocessing.deskew import X`` continues to work for any X
that was importable from the previous flat ``deskew.py``.
"""

from xscore.preprocessing.deskew.anchors import (
    detect_igcse_anchors, extract_igcse_template,
)
from xscore.preprocessing.deskew.angle import (
    detect_writing_line_angle, get_deskew_angle, iterative_deskew_angle,
)
from xscore.preprocessing.deskew.overlay import overlay_reflines_on_pdf
from xscore.preprocessing.deskew.page import (
    deskew_page_halves, deskew_whole_page,
)
from xscore.preprocessing.deskew.pdf_io import (
    deskew_pdf_raster, detect_page_anchors_for_cleaned_scan,
)
from xscore.preprocessing.deskew.reflines import detect_reference_lines
from xscore.preprocessing.deskew.sidecar import (
    anchors_sidecar_path, resolve_deskew_sidecar,
)
from xscore.preprocessing.deskew.types import AnchorPoint, ReferenceLine
from xscore.preprocessing.deskew.warp import deskew_image

__all__ = [
    "AnchorPoint",
    "ReferenceLine",
    "anchors_sidecar_path",
    "deskew_image",
    "deskew_page_halves",
    "deskew_pdf_raster",
    "deskew_whole_page",
    "detect_igcse_anchors",
    "detect_page_anchors_for_cleaned_scan",
    "detect_reference_lines",
    "detect_writing_line_angle",
    "extract_igcse_template",
    "get_deskew_angle",
    "iterative_deskew_angle",
    "overlay_reflines_on_pdf",
    "resolve_deskew_sidecar",
]
