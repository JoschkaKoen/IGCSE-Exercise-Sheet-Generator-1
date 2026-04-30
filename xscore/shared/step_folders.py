"""Step-folder name constants for per-exam derived artifacts.

These constants name the subdirectories under
``output/xscore/<stem>/<timestamp>/`` that hold the artifacts written by each
pipeline step. Path builders that combine these with an ``artifact_dir`` live
in :mod:`xscore.shared.path_builders`; ``exam_paths`` re-exports both modules
for backwards compatibility.
"""

from __future__ import annotations


SUBDIR_INPUT       = "input"   # copies of all input files used by this run

# Fixed-step subdirectories
STEP_01 = "01_parse_grading_instructions"
STEP_03 = "03_read_student_list"
STEP_05 = "05_detect_blank_pages"
STEP_06 = "06_autorotate"
STEP_07 = "07_deskew"

# ── New ordering (post-refactor) ─────────────────────────────────────────────
# Steps 8–9: empty-exam analysis (no scan dependency).
STEP_08_LAYOUT          = "08_detect_exam_layout"
STEP_09_CUT             = "09_cut_exam"
# Steps 10–12: cover detection + scan geometry.
STEP_10_COVER_EMPTY     = "10_cover_page_empty"
STEP_11_COVER_SCAN      = "11_cover_page_scan"
STEP_12_GEOMETRY        = "12_exam_geometry"
# Step 13: per-page vision classification (replaces old step 15).
STEP_13_HANDWRITING     = "13_student_handwriting"
# Step 14: cover-anchored student-name detection (was old step 12).
STEP_14_NAMES           = "14_student_names"
# Step 15: heuristic page-order check (was old step 13).
STEP_15_PAGE_ORDER      = "15_page_order"
# Step 16: empty-exam blank-page detection (was old step 14).
STEP_16_EXAM_BLANK      = "16_exam_blank_detection"
# Step 17: build marking page register v1 (data transform).
STEP_17_BUILD_REGISTER  = "17_build_marking_register"
# Steps 18–19: split of old step 18 (parse_exam_pdf) into detect + fill phases.
STEP_18_DETECT_SCAFFOLD = "18_detect_exam_scaffold"
STEP_19_FILL_SCAFFOLD   = "19_fill_exam_scaffold"
# Step 20: cross-page context augmentation (was old step 19).
STEP_20_CROSS_PAGE_CONTEXT = "20_detect_cross_page_context"
# Marking pipeline (each shifted by +1 from the old numbering).
STEP_21_GRAPHICS         = "21_detect_mark_scheme_graphics"
STEP_22_ASSIGN_QUESTIONS = "22_assign_scheme_questions"
STEP_23_PARSE_SCHEME     = "23_parse_mark_scheme"
STEP_24_CREATE_REPORT    = "24_create_report"
STEP_25_BLUEPRINTS       = "25_ai_marking_blueprints"
STEP_26_AI_MARKING       = "26_ai_marking"
STEP_27_STUDENT_REPORTS  = "27_student_report_preparation"
STEP_28_CLASS_STATS      = "28_class_stats"
STEP_29_STUDENT_PDFS     = "29_student_pdfs"
STEP_30_CLASS_REPORT     = "30_class_report"
STEP_31_REVIEW_QUEUE     = "31_review_queue"
STEP_32_TIMING           = "32_timing_summary"
STEP_33_ACCURACY         = "33_accuracy"
STEP_34_AI_COSTS         = "34_ai_costs"

# ---------------------------------------------------------------------------
# Backwards-compat aliases — old constant names point at the NEW folder
# strings so callers that import the old name keep resolving to the right
# (renumbered) folder. Drop these once every importer has been migrated.
# ---------------------------------------------------------------------------
# Pre-refactor names (old step numbers) → new constants
STEP_08_COVER_EMPTY  = STEP_10_COVER_EMPTY
STEP_09_COVER_SCAN   = STEP_11_COVER_SCAN
STEP_10_GEOMETRY     = STEP_12_GEOMETRY
STEP_12_NAMES        = STEP_14_NAMES
STEP_13_PAGE_ORDER   = STEP_15_PAGE_ORDER
STEP_14_EXAM_BLANK   = STEP_16_EXAM_BLANK
STEP_15_HANDWRITING  = STEP_13_HANDWRITING
STEP_16_LAYOUT       = STEP_08_LAYOUT
STEP_17_CUT          = STEP_09_CUT
# Step 18 split — old monolithic name maps to the *fill* step (the one that
# produces exam_questions); the new detect step gets its own constant.
STEP_18_PARSE_EXAM   = STEP_19_FILL_SCAFFOLD
STEP_19_CROSS_PAGE_CONTEXT = STEP_20_CROSS_PAGE_CONTEXT
STEP_19_CROSS_PAGE_FIGURES = STEP_20_CROSS_PAGE_CONTEXT
STEP_19_GRAPHICS         = STEP_21_GRAPHICS
STEP_20_GRAPHICS         = STEP_21_GRAPHICS
STEP_20_ASSIGN_QUESTIONS = STEP_22_ASSIGN_QUESTIONS
STEP_21_ASSIGN_QUESTIONS = STEP_22_ASSIGN_QUESTIONS
STEP_21_PARSE_SCHEME     = STEP_23_PARSE_SCHEME
STEP_22_PARSE_SCHEME     = STEP_23_PARSE_SCHEME
STEP_22_CREATE_REPORT    = STEP_24_CREATE_REPORT
STEP_23_CREATE_REPORT    = STEP_24_CREATE_REPORT
STEP_23_BLUEPRINTS       = STEP_25_BLUEPRINTS
STEP_24_BLUEPRINTS       = STEP_25_BLUEPRINTS
STEP_24_AI_MARKING       = STEP_26_AI_MARKING
STEP_25_AI_MARKING       = STEP_26_AI_MARKING
STEP_25_STUDENT_REPORTS  = STEP_27_STUDENT_REPORTS
STEP_26_STUDENT_REPORTS  = STEP_27_STUDENT_REPORTS
STEP_26_CLASS_STATS      = STEP_28_CLASS_STATS
STEP_27_CLASS_STATS      = STEP_28_CLASS_STATS
STEP_27_STUDENT_PDFS     = STEP_29_STUDENT_PDFS
STEP_28_STUDENT_PDFS     = STEP_29_STUDENT_PDFS
STEP_28_CLASS_REPORT     = STEP_30_CLASS_REPORT
STEP_29_CLASS_REPORT     = STEP_30_CLASS_REPORT
STEP_29_REVIEW_QUEUE     = STEP_31_REVIEW_QUEUE
STEP_30_REVIEW_QUEUE     = STEP_31_REVIEW_QUEUE
STEP_30_TIMING           = STEP_32_TIMING
STEP_31_TIMING           = STEP_32_TIMING
STEP_31_ACCURACY         = STEP_33_ACCURACY
STEP_32_ACCURACY         = STEP_33_ACCURACY
STEP_32_AI_COSTS         = STEP_34_AI_COSTS
STEP_33_AI_COSTS         = STEP_34_AI_COSTS

# Older umbrella alias kept for the resume-artifact copier and any external
# script that references the old name. Now points to the renumbered folder.
STEP_25_COMPILE_REPORTS = STEP_27_STUDENT_REPORTS

# Path of cleaned scan relative to artifact_dir (updated from "7_cleaned_scan.pdf")
CLEANED_SCAN_PDF = STEP_07 + "/cleaned_scan.pdf"

# Backwards-compatible aliases kept for callers not yet migrated to per-step paths
SUBDIR_STUDENTS = "students"
SUBDIR_NAMES    = STEP_14_NAMES + "/names"
