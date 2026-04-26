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
STEP_19_GRAPHICS         = "19_detect_mark_scheme_graphics"
STEP_20_ASSIGN_QUESTIONS = "20_assign_scheme_questions"
STEP_21_PARSE_SCHEME     = "21_parse_mark_scheme"
STEP_22_CREATE_REPORT    = "22_create_report"
STEP_23_BLUEPRINTS       = "23_ai_marking_blueprints"
STEP_24_AI_MARKING       = "24_ai_marking"
STEP_25_STUDENT_REPORTS  = "25_student_report_preparation"
STEP_26_CLASS_STATS      = "26_class_stats"
STEP_27_STUDENT_PDFS     = "27_student_pdfs"
STEP_28_CLASS_REPORT     = "28_class_report"
STEP_29_REVIEW_QUEUE     = "29_review_queue"
STEP_30_TIMING           = "30_timing_summary"
STEP_31_ACCURACY         = "31_accuracy"
STEP_32_AI_COSTS         = "32_ai_costs"

# Backwards-compat alias kept for the resume-artifact copier and any
# external script that references the old umbrella name. Old runs continue
# to use the legacy "23_compile_reports/" folder; new runs split across 25–29.
STEP_25_COMPILE_REPORTS = STEP_25_STUDENT_REPORTS

# Path of cleaned scan relative to artifact_dir (updated from "7_cleaned_scan.pdf")
CLEANED_SCAN_PDF = STEP_07 + "/cleaned_scan.pdf"

# Backwards-compatible aliases kept for callers not yet migrated to per-step paths
SUBDIR_STUDENTS = "students"
SUBDIR_NAMES    = STEP_12_NAMES + "/names"
