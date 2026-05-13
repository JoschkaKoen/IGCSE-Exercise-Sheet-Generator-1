"""Step-folder name constants for per-exam derived artifacts.

These constants name the subdirectories under
``output/xscore/<stem>/<timestamp>/`` that hold the artifacts written by each
pipeline step. Folder strings are the canonical ``NN_<step.name>`` form: the
``NN_`` prefix matches the step's ``number`` and the rest matches its ``name``.

Path builders that combine these with an ``artifact_dir`` live in
:mod:`xscore.shared.path_builders`; ``exam_paths`` re-exports both modules
for backwards compatibility.
"""

from __future__ import annotations


SUBDIR_INPUT = "input"   # copies of all input files used by this run

# Prompt, folder & roster.
PARSE_INSTRUCTIONS_DIR        = "01_parse_grading_instructions"
STUDENT_LIST_DIR              = "03_read_student_list"

# Scan cleaning.
PREPARE_SCANS_DIR             = "04_prepare_scans"
DESKEW_DIR                    = "05_deskew"

# Empty-exam analysis (no scan dependency).
LAYOUT_DIR                    = "06_detect_exam_layout"
CUT_EXAM_DIR                  = "07_cut_exam_pdf"

# Cover detection + scan geometry.
COVER_EMPTY_DIR               = "08_cover_page_empty_exam"
COVER_SCAN_DIR                = "09_cover_page_scan_first"
GEOMETRY_DIR                  = "10_exam_geometry"
DETECT_SUBJECT_DIR            = "11_detect_subject"

# Per-page validation.
EMPTY_EXAM_CLASSIFY_DIR       = "12_classify_empty_exam_pages"
HANDWRITING_DIR               = "13_student_handwriting_check"
STUDENT_NAMES_DIR             = "14_student_names"
PAGE_ORDER_DIR                = "15_page_order_check"
BUILD_REGISTER_DIR            = "16_build_marking_register_v1"

# Empty-exam parse (question numbers + per-question text/options).
EXTRACT_QUESTION_NUMBERS_DIR  = "17_extract_exam_question_numbers"
EXTRACT_QUESTIONS_DIR         = "18_extract_exam_questions"
CROSS_PAGE_CONTEXT_DIR        = "19_detect_cross_page_context"

# Mark scheme parsing.
SCHEME_GRAPHICS_DIR           = "20_detect_mark_scheme_graphics"
ASSIGN_QUESTIONS_DIR          = "21_assign_scheme_questions"
PARSE_SCHEME_DIR              = "22_parse_mark_scheme"
TRANSCRIBE_SCHEME_GRAPHICS_DIR = "23_transcribe_scheme_graphics"
CREATE_REPORT_DIR             = "24_create_report"

# AI marking.
BLUEPRINTS_DIR                = "25_ai_marking_blueprints"
EXTRACT_ANSWERS_DIR           = "26_extract_student_answers"
AI_MARKING_DIR                = "27_ai_marking"

# Reports & PDFs.
STUDENT_REPORTS_DIR           = "28_per_student_reports"
CLASS_STATS_DIR               = "29_class_stats_curve"
STUDENT_PDFS_DIR              = "30_per_student_pdfs"
CLASS_REPORT_DIR              = "31_class_report"
REVIEW_QUEUE_DIR              = "32_review_queue"

# Summary.
TIMING_DIR                    = "33_timing_summary"
AI_COSTS_DIR                  = "34_ai_costs"


# Path of the merged + angle-adjusted scan relative to artifact_dir (run root).
CLEANED_SCAN_PDF = "scanned_exam_merged_and_angles_adjusted.pdf"

# Backwards-compatible aliases kept for callers not yet migrated to per-step paths.
SUBDIR_STUDENTS = "students"
SUBDIR_NAMES    = STUDENT_NAMES_DIR + "/names"
