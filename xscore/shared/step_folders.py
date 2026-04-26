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
STEP_08 = "08_exam_geometry"
STEP_09_COVER       = "09_cover_page"
STEP_10_COVER_SCAN  = "10_cover_page_scan"
STEP_11_NAMES       = "11_student_names"
STEP_13_PAGE_ORDER  = "13_page_order"
STEP_14_EXAM_BLANK      = "14_exam_blank_detection"
STEP_15_HANDWRITING     = "15_student_handwriting"

STEP_16_LAYOUT          = "16_detect_exam_layout"
STEP_17_CUT             = "17_cut_exam"
STEP_18_PARSE_EXAM      = "18_parse_exam_pdf"
STEP_19_GRAPHICS        = "19_detect_mark_scheme_graphics"
STEP_20_PARSE_SCHEME    = "20_parse_mark_scheme"
STEP_21_CREATE_REPORT   = "21_create_report"
STEP_22_BLUEPRINTS      = "22_ai_marking_blueprints"
STEP_23_AI_MARKING      = "23_ai_marking"
STEP_24_STUDENT_REPORTS = "24_student_reports"
STEP_25_CLASS_STATS     = "25_class_stats"
STEP_26_STUDENT_PDFS    = "26_student_pdfs"
STEP_27_CLASS_REPORT    = "27_class_report"
STEP_28_REVIEW_QUEUE    = "28_review_queue"
STEP_29_TIMING          = "29_timing_summary"
STEP_30_ACCURACY        = "30_accuracy"
STEP_31_AI_COSTS        = "31_ai_costs"

# Backwards-compat alias kept for the resume-artifact copier and any
# external script that references the old umbrella name. Old runs continue
# to use the legacy "23_compile_reports/" folder; new runs split across 24–28.
STEP_24_COMPILE_REPORTS = STEP_24_STUDENT_REPORTS

# Path of cleaned scan relative to artifact_dir (updated from "7_cleaned_scan.pdf")
CLEANED_SCAN_PDF = STEP_07 + "/cleaned_scan.pdf"

# Backwards-compatible aliases kept for callers not yet migrated to per-step paths
SUBDIR_STUDENTS = "students"
SUBDIR_NAMES    = STEP_11_NAMES + "/names"
