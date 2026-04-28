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
STEP_08_COVER_EMPTY  = "08_cover_page_empty"
STEP_09_COVER_SCAN   = "09_cover_page_scan"
STEP_10_GEOMETRY     = "10_exam_geometry"
STEP_11_COVER_VERIFY = "11_cover_page_verify"
STEP_12_NAMES        = "12_student_names"
STEP_13_PAGE_ORDER   = "13_page_order"
STEP_14_EXAM_BLANK      = "14_exam_blank_detection"
STEP_15_HANDWRITING     = "15_student_handwriting"

STEP_16_LAYOUT          = "16_detect_exam_layout"
STEP_17_CUT             = "17_cut_exam"
STEP_18_PARSE_EXAM      = "18_parse_exam_pdf"

# Step 19 (NEW): build the marking page register augmented with cross-page
# figure references (e.g. "Fig. 1.1" referenced on a different page than the
# one it's drawn on). All steps from the previous step 19 onward shift by +1.
STEP_19_CROSS_PAGE_FIGURES = "19_detect_cross_page_figures"
STEP_20_GRAPHICS         = "20_detect_mark_scheme_graphics"
STEP_21_ASSIGN_QUESTIONS = "21_assign_scheme_questions"
STEP_22_PARSE_SCHEME     = "22_parse_mark_scheme"
STEP_23_CREATE_REPORT    = "23_create_report"
STEP_24_BLUEPRINTS       = "24_ai_marking_blueprints"
STEP_25_AI_MARKING       = "25_ai_marking"
STEP_26_STUDENT_REPORTS  = "26_student_report_preparation"
STEP_27_CLASS_STATS      = "27_class_stats"
STEP_28_STUDENT_PDFS     = "28_student_pdfs"
STEP_29_CLASS_REPORT     = "29_class_report"
STEP_30_REVIEW_QUEUE     = "30_review_queue"
STEP_31_TIMING           = "31_timing_summary"
STEP_32_ACCURACY         = "32_accuracy"
STEP_33_AI_COSTS         = "33_ai_costs"

# ---------------------------------------------------------------------------
# Backwards-compat aliases — old constant names point to the NEW folder
# strings so callers that imported the old name keep resolving to the right
# (renumbered) folder. Drop these once every importer has been migrated.
# ---------------------------------------------------------------------------
STEP_19_GRAPHICS         = STEP_20_GRAPHICS
STEP_20_ASSIGN_QUESTIONS = STEP_21_ASSIGN_QUESTIONS
STEP_21_PARSE_SCHEME     = STEP_22_PARSE_SCHEME
STEP_22_CREATE_REPORT    = STEP_23_CREATE_REPORT
STEP_23_BLUEPRINTS       = STEP_24_BLUEPRINTS
STEP_24_AI_MARKING       = STEP_25_AI_MARKING
STEP_25_STUDENT_REPORTS  = STEP_26_STUDENT_REPORTS
STEP_26_CLASS_STATS      = STEP_27_CLASS_STATS
STEP_27_STUDENT_PDFS     = STEP_28_STUDENT_PDFS
STEP_28_CLASS_REPORT     = STEP_29_CLASS_REPORT
STEP_29_REVIEW_QUEUE     = STEP_30_REVIEW_QUEUE
STEP_30_TIMING           = STEP_31_TIMING
STEP_31_ACCURACY         = STEP_32_ACCURACY
STEP_32_AI_COSTS         = STEP_33_AI_COSTS

# Older umbrella alias kept for the resume-artifact copier and any external
# script that references the old name. Now points to the renumbered folder.
STEP_25_COMPILE_REPORTS = STEP_26_STUDENT_REPORTS

# Path of cleaned scan relative to artifact_dir (updated from "7_cleaned_scan.pdf")
CLEANED_SCAN_PDF = STEP_07 + "/cleaned_scan.pdf"

# Backwards-compatible aliases kept for callers not yet migrated to per-step paths
SUBDIR_STUDENTS = "students"
SUBDIR_NAMES    = STEP_12_NAMES + "/names"
