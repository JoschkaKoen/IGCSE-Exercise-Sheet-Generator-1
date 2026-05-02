"""Step-folder name constants for per-exam derived artifacts.

These constants name the subdirectories under
``output/xscore/<stem>/<timestamp>/`` that hold the artifacts written by each
pipeline step. The folder STRINGS keep their numeric ``NN_`` prefix for
chronological browsing in the artifact tree; the constant NAMES describe
what the folder holds.

Path builders that combine these with an ``artifact_dir`` live in
:mod:`xscore.shared.path_builders`; ``exam_paths`` re-exports both modules
for backwards compatibility.
"""

from __future__ import annotations


SUBDIR_INPUT = "input"   # copies of all input files used by this run

PARSE_INSTRUCTIONS_DIR = "01_parse_grading_instructions"
STUDENT_LIST_DIR       = "03_read_student_list"
MERGE_DUPLEX_DIR       = "04_merge_duplex_scans"
BLANK_DETECT_DIR       = "05_detect_blank_pages"
AUTOROTATE_DIR         = "06_autorotate"
DESKEW_DIR             = "07_deskew"

# Empty-exam analysis (no scan dependency).
LAYOUT_DIR             = "08_detect_exam_layout"
CUT_EXAM_DIR           = "09_cut_exam"

# Cover detection + scan geometry.
COVER_EMPTY_DIR        = "10_cover_page_empty"
COVER_SCAN_DIR         = "11_cover_page_scan"
GEOMETRY_DIR           = "12_exam_geometry"
DETECT_SUBJECT_DIR     = "13_detect_subject"

# Per-page validation.
HANDWRITING_DIR        = "14_student_handwriting"
STUDENT_NAMES_DIR      = "15_student_names"
PAGE_ORDER_DIR         = "16_page_order"
EXAM_BLANK_DIR         = "17_exam_blank_detection"
BUILD_REGISTER_DIR     = "18_build_marking_register"

# Empty-exam parse (question numbers + per-question text/options).
EXTRACT_QUESTION_NUMBERS_DIR = "19_extract_exam_question_numbers"
EXTRACT_QUESTIONS_DIR        = "20_extract_exam_questions"
CROSS_PAGE_CONTEXT_DIR = "21_detect_cross_page_context"

# Mark scheme parsing.
SCHEME_GRAPHICS_DIR        = "22_detect_mark_scheme_graphics"
ASSIGN_QUESTIONS_DIR       = "23_assign_scheme_questions"
PARSE_SCHEME_DIR           = "24_parse_mark_scheme"
TRANSCRIBE_SCHEME_GRAPHICS_DIR = "25_transcribe_scheme_graphics"
CREATE_REPORT_DIR          = "26_create_report"

# AI marking.
BLUEPRINTS_DIR         = "27_ai_marking_blueprints"
EXTRACT_ANSWERS_DIR    = "28_extract_student_answers"
AI_MARKING_DIR         = "29_ai_marking"

# Reports & PDFs.
STUDENT_REPORTS_DIR    = "30_student_report_preparation"
CLASS_STATS_DIR        = "31_class_stats"
STUDENT_PDFS_DIR       = "32_student_pdfs"
CLASS_REPORT_DIR       = "33_class_report"
REVIEW_QUEUE_DIR       = "34_review_queue"

# Summary.
TIMING_DIR             = "35_timing_summary"
ACCURACY_DIR           = "36_accuracy"
AI_COSTS_DIR           = "37_ai_costs"


# Path of cleaned scan relative to artifact_dir (run root).
CLEANED_SCAN_PDF = "cleaned_scan.pdf"
# Legacy location used by pre-2026 runs that wrote into the deskew step folder.
# Resume code falls back to this when the run-root copy is missing.
LEGACY_CLEANED_SCAN_PDF = DESKEW_DIR + "/cleaned_scan.pdf"

# Backwards-compatible aliases kept for callers not yet migrated to per-step paths.
SUBDIR_STUDENTS = "students"
SUBDIR_NAMES    = STUDENT_NAMES_DIR + "/names"
