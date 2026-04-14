# -*- coding: utf-8 -*-
"""
Paths and numeric constants for PDF extraction and layout.

**Units:** Unless noted, layout values are **PDF points** (1 pt = 1/72 inch). PDF page
coordinates use the origin at the **top-left** of the page: ``y`` increases **downward**.
Raster crops multiply points by ``DPI / 72`` to get pixels.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (filesystem)
# ---------------------------------------------------------------------------

# Directory that contains this repo (the project root, e.g. the ``eXercise`` folder).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Default folder for generated PDFs; natural-language mode also uses timestamped
# subfolders like ``output/run_YYYYMMDD_HHMMSS/`` for bare output filenames.
OUTPUT_DIR = PROJECT_ROOT / "output"

# Bundled question papers (Cambridge-style PDFs) shipped with this repo under ``exams/``.
PHYSICS_EXAM_DIR = PROJECT_ROOT / "exams" / "physics"
COMPUTER_SCIENCE_EXAM_DIR = PROJECT_ROOT / "exams" / "computer_science"
MATHEMATICS_EXAM_DIR = PROJECT_ROOT / "exams" / "mathematics"
BIOLOGY_EXAM_DIR = PROJECT_ROOT / "exams" / "biology"
CHEMISTRY_EXAM_DIR = PROJECT_ROOT / "exams" / "chemistry"
A_LEVEL_COMPUTER_SCIENCE_EXAM_DIR = PROJECT_ROOT / "exams" / "a_level_computer_science"

# Maps NL/JSON subject keys to the folders above.
EXAM_ROOT_BY_KEY = {
    "physics": PHYSICS_EXAM_DIR,
    "computer_science": COMPUTER_SCIENCE_EXAM_DIR,
    "mathematics": MATHEMATICS_EXAM_DIR,
    "biology": BIOLOGY_EXAM_DIR,
    "chemistry": CHEMISTRY_EXAM_DIR,
    "a_level_computer_science": A_LEVEL_COMPUTER_SCIENCE_EXAM_DIR,
}

# ---------------------------------------------------------------------------
# Rasterization
# ---------------------------------------------------------------------------

# Resolution for rendering PDF pages to images (question papers, mark schemes). Higher
# → sharper output and larger files; must match what you care about for printing.
DPI = 1200

# ---------------------------------------------------------------------------
# Output PDF page size (A4 in points — matches raster layout)
# ---------------------------------------------------------------------------

A4_WIDTH_PT = 595.0
A4_HEIGHT_PT = 842.0

# Font size (pt) for ALL paper labels drawn on exercise/answer sheets:
# the top-of-page header band AND the inline "─── paper ───" section dividers.
EXAM_LABEL_FONT_PT = 9

# Distance (pt) from the very top of each output page to the top edge of the
# first label band.  Reduce to move the label (and all content) higher.
EXAM_LABEL_TOP_PT = 15

# Left margin (pt) of the output page.  Content and labels are inset by this
# amount from the left paper edge.  Reduce to use more of the page width.
OUTPUT_MARGIN_PT = 10

# Right margin (pt) of the output page.  Kept slightly smaller than the left
# margin because exam content is inherently left-weighted (question numbers,
# diagrams) and the extra space on the right is rarely needed.
OUTPUT_MARGIN_RIGHT_PT = 4

# Centered **page header** string when ``exam_key`` is known (natural-language runs).
PAGE_HEADER_BY_EXAM = {
    "physics": "IGCSE Physics",
    "computer_science": "IGCSE Computer Science",
    "mathematics": "IGCSE Mathematics",
    "biology": "IGCSE Biology",
    "chemistry": "IGCSE Chemistry",
    "a_level_computer_science": "A-Level Computer Science",
}

# ---------------------------------------------------------------------------
# Constants that do NOT vary per subject (page layout, QR detection, MS geometry)
# ---------------------------------------------------------------------------

# Below this page **height** (pt), a page is handled as landscape mark scheme; above it,
# portrait rules apply in ``find_ms_answer_regions`` / cropping.
MS_LANDSCAPE_H_THRESHOLD_PT = 700.0

# Left edge of the answer table (pt): crop away everything to the **left** (page margin,
# "0625/41" label area). Raster crop starts at this x.
MS_TABLE_LEFT_PT = 55.0

# Left edge of the "Marks" column (pt): crop everything from this x **rightward** so the
# Marks column is removed; content is ``MS_TABLE_LEFT_PT … MS_MARKS_START_PT``.
MS_MARKS_START_PT = 739.0

# After cropping the table band, the bitmap is scaled to fit a content width of
# ``page_width − 2 * MS_LANDSCAPE_MARGIN_PT`` (in **points**, then scaled like other pt).
# This sets equal left/right **whitespace** on the output page, not the source crop.
MS_LANDSCAPE_MARGIN_PT = 50.0

# Left x (pt) of the portrait mark-scheme table — crop everything to the left.
MS_PORTRAIT_TABLE_LEFT_PT = 46.0

# Right x (pt) where the Marks column begins on portrait MS pages — crop
# everything from this x rightward so the Marks column is excluded.
MS_PORTRAIT_MARKS_START_PT = 500.0

# Embedded or corner-detected squares larger than this side (pt) are not treated as QR.
QR_MAX_SIZE_PT = 90.0

# Only candidates whose bbox lies within this distance (pt) of any page edge are blanked.
QR_MARGIN_ZONE_PT = 90.0

# ---------------------------------------------------------------------------
# Per-subject configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubjectConfig:
    """
    Per-subject constants for extraction tuning.

    All values default to the physics/CS baseline that the pipeline was originally
    calibrated against.  Mathematics (and any future subject) can override only the
    fields that differ, keeping unrelated subjects fully isolated.
    """

    # --- Question paper: question detection ---
    # Vertical band within which question-number text is expected.
    margin_top: float = 55.0
    margin_bottom: float = 790.0
    # Only lines starting at x ≤ this value are candidate question numbers.
    question_x_max: float = 60.0
    # Font-size range for question-number spans.
    font_size_min: float = 9.0
    font_size_max: float = 13.0
    # Start each extracted region a few points above the detected question number.
    padding_above: float = 8.0

    # --- Question paper: strip cropping ---
    # Left/right margins removed on every portrait strip (barcode / "Do not write" area).
    strip_crop_left_pt: float = 45.0
    strip_crop_right_pt: float = 22.0
    # Top crop applied only when the strip starts at or above header_zone_max_y_pt.
    strip_crop_top_pt: float = 8.0
    # Strips starting at or above this y are considered to be in the page-header zone.
    header_zone_max_y_pt: float = 110.0

    # --- Mark scheme: answer-page detection ---
    # Column keyword that identifies mark-scheme table pages (e.g. "Marks" or "Answer").
    ms_marks_column_keyword: str = "Marks"

    # --- Mark scheme: answer-region vertical bounds ---
    # Minimum y where extracted answer-table content begins (excludes column headers).
    ms_header_bottom_pt: float = 74.0
    # On landscape MS pages, do not extend answer regions below this y (avoids footer).
    ms_footer_top_pt: float = 540.0

    # --- Mark scheme: answer-table display margin ---
    # Horizontal margin (pt) on each side of the output page for **landscape** MS
    # answer tables.  The table is scaled to fill ``A4_WIDTH - 2 * margin`` and
    # centred.  Set to ``None`` to use the global ``MS_LANDSCAPE_MARGIN_PT`` default.
    ms_answer_landscape_margin_pt: float | None = 15.0
    # Horizontal margin (pt) for **portrait** MS answer tables.  Set to ``None``
    # to keep tables at native (1:1) width, centred — useful for slim tables
    # (mathematics) or tables that should not be scaled (CS paper-22 style).
    ms_answer_portrait_margin_pt: float | None = None

    # --- Mark scheme: _tight_y_end cropping ---
    # Padding added to the bottom of the last wide drawing (table border line).
    # Zero is correct: the drawing rect's y1 already covers the full stroke,
    # so clipping at exactly y1 includes the border without pulling in the
    # next cell's top edge (~1 px overshoot at 300 DPI with the old 0.25 value).
    drawing_bottom_pad_pt: float = 0.0
    # Trailing gap when a header-cap has already been applied (content is pre-trimmed).
    trailing_gap_capped_pt: float = 20.0
    # Trailing gap when no cap was active (closing border may sit further below last text).
    trailing_gap_uncapped_pt: float = 32.0
    # Minimum drawing width (pt) to be considered a table border.
    drawing_min_width_pt: float = 50.0



# Default config — physics and CS use this unchanged.
DEFAULT_SUBJECT_CONFIG = SubjectConfig()

# Per-subject overrides.  Mathematics starts with the same defaults; override fields
# here as bugs are diagnosed and constants are tuned.
SUBJECT_CONFIG: dict[str, SubjectConfig] = {
    "physics": SubjectConfig(ms_answer_portrait_margin_pt=25.0),
    "computer_science": SubjectConfig(
        ms_answer_landscape_margin_pt=0.0,
    ),
    "mathematics": DEFAULT_SUBJECT_CONFIG,
    "biology": DEFAULT_SUBJECT_CONFIG,
    "chemistry": DEFAULT_SUBJECT_CONFIG,
    "a_level_computer_science": DEFAULT_SUBJECT_CONFIG,
}


def get_subject_config(exam_key: str | None) -> SubjectConfig:
    """Return the SubjectConfig for *exam_key*, falling back to the default."""
    if exam_key and exam_key in SUBJECT_CONFIG:
        return SUBJECT_CONFIG[exam_key]
    return DEFAULT_SUBJECT_CONFIG
