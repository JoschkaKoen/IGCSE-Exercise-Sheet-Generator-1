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
STEP_14_BLANK_PAGES = "14_blank_pages"

STEP_15_LAYOUT          = "15_detect_exam_layout"
STEP_16_CUT             = "16_cut_exam"
STEP_17_PARSE_EXAM      = "17_parse_exam_pdf"
STEP_18_GRAPHICS        = "18_detect_mark_scheme_graphics"
STEP_19_PARSE_SCHEME    = "19_parse_mark_scheme"
STEP_20_CREATE_REPORT   = "20_create_report"
STEP_21_BLUEPRINTS      = "21_ai_marking_blueprints"
STEP_22_AI_MARKING      = "22_ai_marking"
STEP_23_STUDENT_REPORTS = "23_student_reports"
STEP_24_CLASS_STATS     = "24_class_stats"
STEP_25_STUDENT_PDFS    = "25_student_pdfs"
STEP_26_CLASS_REPORT    = "26_class_report"
STEP_27_REVIEW_QUEUE    = "27_review_queue"
STEP_28_TIMING          = "28_timing_summary"
STEP_29_ACCURACY        = "29_accuracy"
STEP_30_AI_COSTS        = "30_ai_costs"

# Backwards-compat alias kept for the resume-artifact copier and any
# external script that references the old umbrella name. Old runs continue
# to use the legacy "23_compile_reports/" folder; new runs split across 23–27.
STEP_23_COMPILE_REPORTS = STEP_23_STUDENT_REPORTS

# Path of cleaned scan relative to artifact_dir (updated from "7_cleaned_scan.pdf")
CLEANED_SCAN_PDF = STEP_07 + "/cleaned_scan.pdf"

# Backwards-compatible aliases kept for callers not yet migrated to per-step paths
SUBDIR_STUDENTS = "students"
SUBDIR_NAMES    = STEP_11_NAMES + "/names"
