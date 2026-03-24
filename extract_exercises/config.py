# -*- coding: utf-8 -*-
"""
Paths and numeric constants for PDF extraction and layout.

**Units:** Unless noted, layout values are **PDF points** (1 pt = 1/72 inch). PDF page
coordinates use the origin at the **top-left** of the page: ``y`` increases **downward**.
Raster crops multiply points by ``DPI / 72`` to get pixels.
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Paths (filesystem)
# ---------------------------------------------------------------------------

# Directory that contains this repo (the "Exercise Sheet Generator" folder).
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Default folder for generated PDFs; natural-language mode also uses timestamped
# subfolders like ``output/run_YYYYMMDD_HHMMSS/`` for bare output filenames.
OUTPUT_DIR = PROJECT_ROOT / "output"

# Bundled question papers (Cambridge-style PDFs) shipped with this repo under ``exams/``.
PHYSICS_EXAM_DIR = PROJECT_ROOT / "exams" / "physics"
COMPUTER_SCIENCE_EXAM_DIR = PROJECT_ROOT / "exams" / "computer_science"

# Maps NL/JSON subject keys to the folders above.
EXAM_ROOT_BY_KEY = {
    "physics": PHYSICS_EXAM_DIR,
    "computer_science": COMPUTER_SCIENCE_EXAM_DIR,
}

# ---------------------------------------------------------------------------
# Rasterization
# ---------------------------------------------------------------------------

# Resolution for rendering PDF pages to images (question papers, mark schemes). Higher
# → sharper output and larger files; must match what you care about for printing.
DPI = 1200

# ---------------------------------------------------------------------------
# Question paper strips (``collect_strips_from_regions`` on portrait pages)
# ---------------------------------------------------------------------------

# If a strip’s **top** starts at or above this ``y`` (pt), treat it as the page header
# band and apply ``STRIP_CROP_TOP_PT`` to shave QR/boilerplate; strips starting lower get
# no top crop so mid-page content is not eaten.
HEADER_ZONE_MAX_Y_PT = 110.0

# Left/right: removed on **every** portrait strip (barcode / “Do not write” margins).
STRIP_CROP_LEFT_PT = 45.0
STRIP_CROP_RIGHT_PT = 22.0

# Top: only when ``y_start <= HEADER_ZONE_MAX_Y_PT``; removes a thin band under the header
# (e.g. separator line). Set to 0 to disable top cropping in that band.
STRIP_CROP_TOP_PT = 8.0

# ---------------------------------------------------------------------------
# Structured mark scheme — landscape table pages
# ---------------------------------------------------------------------------
# Used when ``page.rect.height < MS_LANDSCAPE_H_THRESHOLD_PT`` (typical landscape MS).

# Below this page **height** (pt), a page is handled as landscape mark scheme; above it,
# portrait rules apply in ``find_ms_answer_regions`` / cropping.
MS_LANDSCAPE_H_THRESHOLD_PT = 700.0

# On landscape MS pages, do not extend answer regions below this ``y`` (pt) — avoids the
# copyright/footer block at the bottom of the page.
MS_FOOTER_TOP_PT = 540.0

# Minimum ``y`` (pt) where extracted **answer table** content begins: everything above
# (table title row, column headers like “Question / Answer / Marks”) is excluded. Raise
# this to crop more header; lower to keep more of the table top.
MS_HEADER_BOTTOM_PT = 74.0

# Left edge of the answer table (pt): crop away everything to the **left** (page margin,
# “0625/41” label area). Raster crop starts at this x.
MS_TABLE_LEFT_PT = 55.0

# Left edge of the “Marks” column (pt): crop everything from this x **rightward** so the
# Marks column is removed; content is ``MS_TABLE_LEFT_PT … MS_MARKS_START_PT``.
MS_MARKS_START_PT = 739.0

# After cropping the table band, the bitmap is scaled to fit a content width of
# ``page_width − 2 * MS_LANDSCAPE_MARGIN_PT`` (in **points**, then scaled like other pt).
# This sets equal left/right **whitespace** on the output page, not the source crop.
MS_LANDSCAPE_MARGIN_PT = 50.0

# ---------------------------------------------------------------------------
# Structured mark scheme — portrait table pages
# ---------------------------------------------------------------------------
# Used when ``page.rect.height >= MS_LANDSCAPE_H_THRESHOLD_PT`` AND the
# document is a structured mark scheme (not a question paper).

# Left x (pt) of the portrait mark-scheme table — crop everything to the left.
MS_PORTRAIT_TABLE_LEFT_PT = 46.0

# Right x (pt) where the Marks column begins on portrait MS pages — crop
# everything from this x rightward so the Marks column is excluded.
MS_PORTRAIT_MARKS_START_PT = 500.0

# ---------------------------------------------------------------------------
# QR blanking (before strip crop on rendered pages)
# ---------------------------------------------------------------------------

# Embedded or corner-detected squares larger than this side (pt) are not treated as QR.
QR_MAX_SIZE_PT = 90.0

# Only candidates whose bbox lies within this distance (pt) of any page edge are blanked.
QR_MARGIN_ZONE_PT = 90.0

# ---------------------------------------------------------------------------
# Question detection (``find_question_positions`` / ``get_question_regions``)
# ---------------------------------------------------------------------------

# Ignore text lines whose baseline ``y`` is outside this vertical band (typical question body).
MARGIN_TOP = 55
MARGIN_BOTTOM = 790

# Only consider lines starting at ``x <= QUESTION_X_MAX`` as candidate question numbers
# (left margin where “1”, “2”, … appear).
QUESTION_X_MAX = 60

# When building regions, start each question a few points **above** its detected number.
PADDING_ABOVE = 8

# ---------------------------------------------------------------------------
# Output PDF page size (A4 in points — matches raster layout)
# ---------------------------------------------------------------------------

A4_WIDTH_PT = 595.0
A4_HEIGHT_PT = 842.0

# Point size for the subject line (and related) drawn onto rasterized output pages.
EXAM_LABEL_FONT_PT = 11

# Centered **page header** string when ``exam_key`` is known (natural-language runs).
PAGE_HEADER_BY_EXAM = {
    "physics": "IGCSE Physics",
    "computer_science": "IGCSE Computer Science",
}
