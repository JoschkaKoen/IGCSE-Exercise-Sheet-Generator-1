"""Path builders for per-exam derived artifacts.

All ``artifact_*_path()`` and ``artifact_*_dir()`` functions live here, plus a
few small helpers (``safe_student_name``, ``safe_path_stem``, ``exam_artifact_dir``)
that the builders depend on.

Folder-name constants are imported from :mod:`xscore.shared.step_folders`.
``xscore.shared.exam_paths`` re-exports both modules for backwards compatibility.
"""

from __future__ import annotations

import re
from pathlib import Path

from xscore.shared.step_folders import (
    ACCURACY_DIR,
    AI_COSTS_DIR,
    AI_MARKING_DIR,
    ASSIGN_QUESTIONS_DIR,
    AUTOROTATE_DIR,
    BLANK_DETECT_DIR,
    BLUEPRINTS_DIR,
    BUILD_REGISTER_DIR,
    CLASS_REPORT_DIR,
    CLASS_STATS_DIR,
    CLEANED_SCAN_PDF,
    COVER_EMPTY_DIR,
    COVER_SCAN_DIR,
    CREATE_REPORT_DIR,
    CROSS_PAGE_CONTEXT_DIR,
    CUT_EXAM_DIR,
    DESKEW_DIR,
    DETECT_SCAFFOLD_DIR,
    DETECT_SUBJECT_DIR,
    EXAM_BLANK_DIR,
    EXTRACT_ANSWERS_DIR,
    FILL_SCAFFOLD_DIR,
    GEOMETRY_DIR,
    HANDWRITING_DIR,
    LAYOUT_DIR,
    PAGE_ORDER_DIR,
    PARSE_INSTRUCTIONS_DIR,
    PARSE_SCHEME_DIR,
    REVIEW_QUEUE_DIR,
    SCHEME_GRAPHICS_DIR,
    STUDENT_LIST_DIR,
    STUDENT_NAMES_DIR,
    STUDENT_PDFS_DIR,
    STUDENT_REPORTS_DIR,
    SUBDIR_INPUT,
    SUBDIR_NAMES,
    SUBDIR_STUDENTS,
    TIMING_DIR,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_student_name(name: str) -> str:
    """Replace every non-word character in *name* with an underscore."""
    return re.sub(r"[^\w]", "_", name)


def safe_path_stem(stem: str) -> str:
    """Stable directory/filename fragment from a PDF stem."""
    stem = stem.replace("\x00", "").replace(" ", "_").replace("/", "_").replace("\\", "_")
    parts = stem.split("_")
    parts = [p if p != ".." else "__" for p in parts]
    return "_".join(parts) or "_"


def exam_artifact_dir(exam_folder: Path, output_base: str | Path = "output/xscore") -> Path:
    """Directory for all per-exam artifacts (``output/xscore/<stem>/``)."""
    stem = exam_folder.name.replace(" ", "_")
    return Path(output_base) / stem


# ---------------------------------------------------------------------------
# Input copies (folder selection)
# ---------------------------------------------------------------------------

def artifact_input_dir(artifact_dir: Path) -> Path:
    """Directory that receives copies of all input files used by this run."""
    return artifact_dir / SUBDIR_INPUT


# ---------------------------------------------------------------------------
# Parse grading instructions
# ---------------------------------------------------------------------------

def artifact_parse_summary_path(artifact_dir: Path) -> Path:
    return artifact_dir / PARSE_INSTRUCTIONS_DIR / "summary.json"


def artifact_parse_prompt_path(artifact_dir: Path) -> Path:
    """Parse-instruction prompt JSON; the matching response file is
    written by ``save_response`` as ``parse_prompt_response.txt`` alongside it.
    """
    return artifact_dir / PARSE_INSTRUCTIONS_DIR / "parse_prompt.json"


# ---------------------------------------------------------------------------
# Read student list
# ---------------------------------------------------------------------------

def artifact_students_json_path(artifact_dir: Path) -> Path:
    """Student roster as a JSON array of name strings."""
    return artifact_dir / STUDENT_LIST_DIR / "students.json"


def artifact_students_markdown_path(artifact_dir: Path) -> Path:
    """Human-readable numbered student list."""
    return artifact_dir / STUDENT_LIST_DIR / "students.md"


def artifact_student_list_prompt_path(artifact_dir: Path) -> Path:
    """Prompt file for student-list AI call."""
    return artifact_dir / STUDENT_LIST_DIR / "student_list_prompt.md"


# ---------------------------------------------------------------------------
# Cover page detection (empty exam)
# ---------------------------------------------------------------------------

def artifact_cover_page_dir(artifact_dir: Path) -> Path:
    """Directory for empty-exam cover-page detection artifacts."""
    return artifact_dir / COVER_EMPTY_DIR


# ---------------------------------------------------------------------------
# Cover page detection (scan, first page only)
# ---------------------------------------------------------------------------

def artifact_cover_scan_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Prompt file for scan first-page cover detection."""
    return artifact_dir / COVER_SCAN_DIR / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Scan geometry (pages per student)
# ---------------------------------------------------------------------------

def artifact_geometry_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / GEOMETRY_DIR / "exam_geometry.json"


def artifact_geometry_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / GEOMETRY_DIR / "exam_geometry.md"


def artifact_geometry_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Prompt file for geometry AI calls."""
    return artifact_dir / GEOMETRY_DIR / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Subject detection
# ---------------------------------------------------------------------------

def artifact_subject_dir(artifact_dir: Path) -> Path:
    return artifact_dir / DETECT_SUBJECT_DIR


def artifact_subject_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / DETECT_SUBJECT_DIR / "subject.json"


def artifact_subject_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / DETECT_SUBJECT_DIR / "subject.md"


def artifact_subject_prompt_path(artifact_dir: Path, name: str = "subject") -> Path:
    return artifact_dir / DETECT_SUBJECT_DIR / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Student names
# ---------------------------------------------------------------------------

def artifact_exam_student_list_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STUDENT_NAMES_DIR / "exam_student_list.json"


def artifact_exam_student_list_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STUDENT_NAMES_DIR / "exam_student_list.md"


def artifact_exam_page_range_overview_path(artifact_dir: Path) -> Path:
    """Human-readable '<student> page <a>-<b>' overview, one line per student."""
    return artifact_dir / STUDENT_NAMES_DIR / "page_range_overview.txt"


def artifact_names_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Prompt file for name-detection AI calls (one per scan page)."""
    return artifact_dir / STUDENT_NAMES_DIR / "names" / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Page order
# ---------------------------------------------------------------------------

def artifact_page_order_txt_path(artifact_dir: Path, student: str) -> Path:
    """Per-student page-order detection text file."""
    return artifact_dir / PAGE_ORDER_DIR / f"page_order_{safe_student_name(student)}.txt"


def artifact_page_order_prompt_path(artifact_dir: Path, student: str) -> Path:
    """Per-student prompt file for page-order AI call."""
    return artifact_dir / PAGE_ORDER_DIR / f"page_order_{safe_student_name(student)}_prompt.md"


def artifact_page_order_empty_exam_txt_path(artifact_dir: Path) -> Path:
    """Empty-exam page-order detection text file."""
    return artifact_dir / PAGE_ORDER_DIR / "page_order_empty_exam.txt"


# ---------------------------------------------------------------------------
# Exam blank detection (text-only)
# ---------------------------------------------------------------------------

def artifact_exam_blank_json_path(artifact_dir: Path) -> Path:
    """Blank exam pages list JSON."""
    return artifact_dir / EXAM_BLANK_DIR / "blank_exam_pages.json"


def artifact_exam_blank_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Prompt file for exam blank-detection AI calls."""
    return artifact_dir / EXAM_BLANK_DIR / f"{name}_prompt.md"


def artifact_blank_detection_txt_path(artifact_dir: Path) -> Path:
    """Empty-exam blank-detection output text file."""
    return artifact_dir / EXAM_BLANK_DIR / "blank_detection_empty_exam.txt"


# ---------------------------------------------------------------------------
# Student handwriting check (vision)
# ---------------------------------------------------------------------------

def artifact_handwriting_json_path(artifact_dir: Path) -> Path:
    """Per-student handwriting detection results JSON."""
    return artifact_dir / HANDWRITING_DIR / "handwriting.json"


def artifact_handwriting_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Prompt file for handwriting AI calls."""
    return artifact_dir / HANDWRITING_DIR / f"{name}_prompt.md"


def artifact_handwriting_dir(artifact_dir: Path) -> Path:
    """Directory of JPEG images rendered for handwriting checks."""
    return artifact_dir / HANDWRITING_DIR / "scan_pages"


def artifact_marking_page_register_v1_path(artifact_dir: Path) -> Path:
    """Initial marking page register (one row per AI marking call)."""
    return artifact_dir / BUILD_REGISTER_DIR / "marking_page_register.json"


# ---------------------------------------------------------------------------
# Detect cross-page context (refines marking page register with
# cross-page figure references AND parent-question stems)
# ---------------------------------------------------------------------------

def artifact_marking_page_register_v2_path(artifact_dir: Path) -> Path:
    """Refined marking page register with cross-page context extras."""
    return artifact_dir / CROSS_PAGE_CONTEXT_DIR / "marking_page_register.json"


def artifact_cross_page_refs_json_path(artifact_dir: Path) -> Path:
    """Diagnostic listing each detected cross-page figure reference."""
    return artifact_dir / CROSS_PAGE_CONTEXT_DIR / "cross_page_refs.json"


def artifact_parent_refs_json_path(artifact_dir: Path) -> Path:
    """Diagnostic listing each detected parent-context reference."""
    return artifact_dir / CROSS_PAGE_CONTEXT_DIR / "parent_refs.json"


def artifact_cross_page_changes_md_path(artifact_dir: Path) -> Path:
    """Human-readable summary of register changes vs v1."""
    return artifact_dir / CROSS_PAGE_CONTEXT_DIR / "changes.md"


# ---------------------------------------------------------------------------
# Detect exam layout
# ---------------------------------------------------------------------------

def artifact_exam_layout_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / LAYOUT_DIR / "exam_layout.json"


def artifact_exam_layout_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / LAYOUT_DIR / "exam_layout.md"


def artifact_exam_layout_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / LAYOUT_DIR / "exam_layout.xml"


def artifact_exam_layout_raw_path(artifact_dir: Path, fmt: str = "json") -> Path:
    """Raw AI response before parsing (layout detection)."""
    return artifact_dir / LAYOUT_DIR / f"exam_layout_raw.{fmt}"


# ---------------------------------------------------------------------------
# Cut exam PDF (split multi-up layout into single logical pages)
# ---------------------------------------------------------------------------

def artifact_split_exam_pdf_path(artifact_dir: Path) -> Path:
    return artifact_dir / CUT_EXAM_DIR / "split_exam.pdf"


# ---------------------------------------------------------------------------
# Cut exam PDF additional output
# ---------------------------------------------------------------------------

def artifact_exam_input_pdf_path(artifact_dir: Path) -> Path:
    """Copy of the original exam PDF (1×1 mode) — ``exam_input.pdf``."""
    return artifact_dir / CUT_EXAM_DIR / "exam_input.pdf"


# ---------------------------------------------------------------------------
# Detect exam scaffold (Phase A — structure only)
# ---------------------------------------------------------------------------

def artifact_exam_scaffold_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    """Intermediate scaffold — number/type/page/subpage/marks, no text."""
    return artifact_dir / DETECT_SCAFFOLD_DIR / f"exam_scaffold.{fmt}"


def artifact_exam_scaffold_raw_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / DETECT_SCAFFOLD_DIR / f"exam_scaffold_raw.{fmt}"


# ---------------------------------------------------------------------------
# Fill exam scaffold (Phase B — text + options per question)
# ---------------------------------------------------------------------------

def artifact_exam_questions_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / FILL_SCAFFOLD_DIR / "exam_questions.json"


def artifact_exam_questions_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / FILL_SCAFFOLD_DIR / "exam_questions.md"


def artifact_exam_questions_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / FILL_SCAFFOLD_DIR / "exam_questions.xml"


def artifact_exam_questions_raw_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / FILL_SCAFFOLD_DIR / "exam_questions_raw.xml"


def artifact_exam_questions_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / FILL_SCAFFOLD_DIR / f"exam_questions.{fmt}"


def artifact_exam_questions_raw_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / FILL_SCAFFOLD_DIR / f"exam_questions_raw.{fmt}"


def artifact_exam_pages_dir(artifact_dir: Path) -> Path:
    """Per-page PDFs from the post-cut exam PDF — produced and consumed by Phase B (fill)."""
    return artifact_dir / FILL_SCAFFOLD_DIR / "pages"


# ---------------------------------------------------------------------------
# Detect mark scheme graphics (per-page splits + graphics detection)
# ---------------------------------------------------------------------------

def artifact_mark_scheme_pages_dir(artifact_dir: Path) -> Path:
    """Per-page PDFs (one per mark scheme page) — produced by detect_mark_scheme_graphics, consumed by assign_scheme_questions and parse_mark_scheme."""
    return artifact_dir / SCHEME_GRAPHICS_DIR / "pages"


def artifact_mark_scheme_graphics_dir(artifact_dir: Path) -> Path:
    """Directory of images extracted from the mark scheme."""
    return artifact_dir / SCHEME_GRAPHICS_DIR / "graphics"


def artifact_mark_scheme_graphics_json_path(artifact_dir: Path) -> Path:
    """Detected graphics positions per question."""
    return artifact_dir / SCHEME_GRAPHICS_DIR / "mark_scheme_graphics.json"


# ---------------------------------------------------------------------------
# Assign questions to mark scheme pages
# ---------------------------------------------------------------------------

def artifact_questions_per_page_path(artifact_dir: Path) -> Path:
    """``{page_num: [question_numbers]}`` JSON used by parse_mark_scheme to filter
    its per-page scaffold to only the relevant questions."""
    return artifact_dir / ASSIGN_QUESTIONS_DIR / "questions_per_page.json"


# ---------------------------------------------------------------------------
# Parse mark scheme
# ---------------------------------------------------------------------------

def artifact_mark_scheme_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / PARSE_SCHEME_DIR / "mark_scheme.json"


def artifact_mark_scheme_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / PARSE_SCHEME_DIR / "mark_scheme.md"


def artifact_mark_scheme_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / PARSE_SCHEME_DIR / "mark_scheme.xml"


def artifact_mark_scheme_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / PARSE_SCHEME_DIR / f"mark_scheme.{fmt}"


# ---------------------------------------------------------------------------
# Create report / scaffold cache
# ---------------------------------------------------------------------------

def artifact_scaffold_xml_path(artifact_dir: Path) -> Path:
    """Merged exam + mark scheme XML scaffold cache."""
    return artifact_dir / CREATE_REPORT_DIR / "report.xml"


def artifact_scaffold_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / CREATE_REPORT_DIR / "report.json"


def artifact_scaffold_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / CREATE_REPORT_DIR / "report.md"


def artifact_short_scaffold_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / CREATE_REPORT_DIR / "short_report.json"


def artifact_short_scaffold_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / CREATE_REPORT_DIR / "short_report.md"


# ---------------------------------------------------------------------------
# AI marking blueprints
# ---------------------------------------------------------------------------

def artifact_blueprint_path(artifact_dir: Path, page: int, fmt: str = "yaml") -> Path:
    return artifact_dir / BLUEPRINTS_DIR / f"blueprint_page_{page}.{fmt}"


def artifact_blueprint_xml_path(artifact_dir: Path, page: int) -> Path:
    return artifact_dir / BLUEPRINTS_DIR / f"blueprint_page_{page}.xml"


def artifact_blueprint_json_path(artifact_dir: Path, page: int) -> Path:
    return artifact_dir / BLUEPRINTS_DIR / f"blueprint_page_{page}.json"


def artifact_blueprint_md_path(artifact_dir: Path, page: int) -> Path:
    return artifact_dir / BLUEPRINTS_DIR / f"blueprint_page_{page}.md"


# ---------------------------------------------------------------------------
# Extract student answers (transcribe verbatim, no marking)
# ---------------------------------------------------------------------------

def artifact_student_answers_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student extracted-answer files."""
    return artifact_dir / EXTRACT_ANSWERS_DIR / "students"


def artifact_student_answers_path(
    artifact_dir: Path, student: str, page: int, *, fmt: str
) -> Path:
    """Per-(student, page) extracted student answers; ``fmt`` is the active
    MARKING_FORMAT (yaml | json | xml). The caller passes
    ``fmt=fmt.artifact_ext()`` so writers and readers stay aligned, and the
    loader (:func:`xscore.marking.extract_answers.load_student_answers`)
    probes all three extensions to remain resilient to format changes
    between runs."""
    return artifact_student_answers_dir(artifact_dir) / f"{safe_student_name(student)}_page_{page}.{fmt}"


def artifact_student_answers_prompt_path(
    artifact_dir: Path, student: str, page: int
) -> Path:
    """Prompt file saved alongside the extraction result for one (student, page)."""
    return artifact_student_answers_dir(artifact_dir) / f"{safe_student_name(student)}_page_{page}_prompt.md"


def artifact_student_answers_failed_path(
    artifact_dir: Path, student: str, page: int
) -> Path:
    """Failure record when all extraction attempts are exhausted for a (student, page)."""
    return artifact_student_answers_dir(artifact_dir) / f"failed_{safe_student_name(student)}_page_{page}.json"


# ---------------------------------------------------------------------------
# AI marking
# ---------------------------------------------------------------------------

def artifact_marking_students_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student marking files."""
    return artifact_dir / AI_MARKING_DIR / "students"


def artifact_marked_path(artifact_dir: Path, student: str, page: int, fmt: str = "yaml") -> Path:
    return artifact_marking_students_dir(artifact_dir) / f"{safe_student_name(student)}_page_{page}.{fmt}"


def artifact_marked_xml_path(artifact_dir: Path, student: str, page: int) -> Path:
    return artifact_marking_students_dir(artifact_dir) / f"{safe_student_name(student)}_page_{page}.xml"


def artifact_marked_md_path(artifact_dir: Path, student: str, page: int) -> Path:
    return artifact_marking_students_dir(artifact_dir) / f"{safe_student_name(student)}_page_{page}.md"


def artifact_marked_failed_path(artifact_dir: Path, student: str, page: int) -> Path:
    """Failure record when all marking attempts are exhausted for a page."""
    return artifact_marking_students_dir(artifact_dir) / f"failed_{safe_student_name(student)}_page_{page}.json"


def artifact_marking_prompt_path(artifact_dir: Path, student: str, page: int) -> Path:
    """Prompt file saved alongside the marking result for one student page."""
    return artifact_marking_students_dir(artifact_dir) / f"{safe_student_name(student)}_page_{page}_prompt.md"


# ---------------------------------------------------------------------------
# Per-student reports (XML + MD)
# ---------------------------------------------------------------------------

def artifact_student_reports_dir(artifact_dir: Path) -> Path:
    """Parent directory holding per-student report subfolders"""
    return artifact_dir / STUDENT_REPORTS_DIR


def artifact_student_report_dir(artifact_dir: Path, student: str) -> Path:
    """Per-student subfolder for XML + Markdown reports"""
    return artifact_student_reports_dir(artifact_dir) / safe_student_name(student)


def artifact_student_report_xml_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_report_dir(artifact_dir, student) / f"{safe_student_name(student)}.xml"


def artifact_student_report_md_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_report_dir(artifact_dir, student) / f"{safe_student_name(student)}.md"


def artifact_student_report_xml_full_path(artifact_dir: Path, student: str) -> Path:
    """Augmented per-student XML — includes blueprint-derived rows for unanswered questions."""
    return artifact_student_report_dir(artifact_dir, student) / f"{safe_student_name(student)}_full.xml"


def artifact_student_report_md_full_path(artifact_dir: Path, student: str) -> Path:
    """Augmented per-student Markdown — companion to the _full XML."""
    return artifact_student_report_dir(artifact_dir, student) / f"{safe_student_name(student)}_full.md"


# Backward-compat alias for callers that haven't migrated to the new name.
artifact_reports_students_dir = artifact_student_reports_dir


# ---------------------------------------------------------------------------
# Class statistics + grade curve
# ---------------------------------------------------------------------------

def artifact_class_stats_json_path(artifact_dir: Path) -> Path:
    """Class average + curve offset, written before per-student PDFs."""
    return artifact_dir / CLASS_STATS_DIR / "class_stats.json"


# ---------------------------------------------------------------------------
# Per-student PDFs (TeX + xelatex output)
# ---------------------------------------------------------------------------

def artifact_student_pdfs_dir(artifact_dir: Path) -> Path:
    """Parent directory holding per-student PDF subfolders"""
    return artifact_dir / STUDENT_PDFS_DIR


def artifact_student_pdf_dir(artifact_dir: Path, student: str) -> Path:
    """Per-student subfolder for .tex + .pdf files"""
    return artifact_student_pdfs_dir(artifact_dir) / safe_student_name(student)


# Layout-variant subfolder names under <student>/. Each layout (and its
# _full / _10pt / _11pt companions) lives in its own folder so the per-
# student directory has six clean groups instead of 50+ flat files.
_VARIANT_LANDSCAPE                 = "landscape"
_VARIANT_LANDSCAPE_WITH_QUESTIONS  = "landscape_with_questions"
_VARIANT_PORTRAIT                  = "portrait"
_VARIANT_PORTRAIT_2UP              = "portrait_2up"
_VARIANT_PORTRAIT_LARGE            = "portrait_large"
_VARIANT_PORTRAIT_LIST             = "portrait_list"


def artifact_student_pdf_variant_dir(
    artifact_dir: Path, student: str, variant: str
) -> Path:
    """Per-student per-variant subfolder (e.g. ``Simon_Wang/landscape``)."""
    return artifact_student_pdf_dir(artifact_dir, student) / variant


def artifact_student_report_tex_landscape_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_LANDSCAPE) / f"{safe_student_name(student)}_landscape.tex"


def artifact_student_report_pdf_landscape_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_LANDSCAPE) / f"{safe_student_name(student)}_landscape.pdf"


def artifact_student_report_tex_portrait_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT) / f"{safe_student_name(student)}_portrait.tex"


def artifact_student_report_pdf_portrait_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT) / f"{safe_student_name(student)}_portrait.pdf"


def artifact_student_report_pdf_portrait_2up_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_2UP) / f"{safe_student_name(student)}_portrait_2up.pdf"


def artifact_student_report_tex_portrait_large_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_LARGE) / f"{safe_student_name(student)}_portrait_large.tex"


def artifact_student_report_pdf_portrait_large_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_LARGE) / f"{safe_student_name(student)}_portrait_large.pdf"


def artifact_student_report_tex_landscape_with_questions_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_LANDSCAPE_WITH_QUESTIONS) / f"{safe_student_name(student)}_landscape_with_questions.tex"


def artifact_student_report_pdf_landscape_with_questions_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_LANDSCAPE_WITH_QUESTIONS) / f"{safe_student_name(student)}_landscape_with_questions.pdf"


def artifact_student_report_tex_portrait_list_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_LIST) / f"{safe_student_name(student)}_portrait_list.tex"


def artifact_student_report_pdf_portrait_list_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_LIST) / f"{safe_student_name(student)}_portrait_list.pdf"


def artifact_exam_questions_tex_path(artifact_dir: Path) -> Path:
    """Standalone exam-questions PDF source — one per run, top-level (top-level)."""
    return artifact_student_pdfs_dir(artifact_dir) / "exam_questions.tex"


def artifact_exam_questions_pdf_path(artifact_dir: Path) -> Path:
    """Standalone exam-questions PDF — one per run, top-level (top-level)."""
    return artifact_student_pdfs_dir(artifact_dir) / "exam_questions.pdf"


# ---------------------------------------------------------------------------
# Class report (XML/MD/TeX/PDF + combined PDF)
# ---------------------------------------------------------------------------

def artifact_class_report_dir(artifact_dir: Path) -> Path:
    return artifact_dir / CLASS_REPORT_DIR


def artifact_class_report_xml_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.xml"


def artifact_class_report_md_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.md"


def artifact_class_marks_xlsx_path(artifact_dir: Path) -> Path:
    """Machine-friendly per-student × per-question marks grid."""
    return artifact_class_report_dir(artifact_dir) / "class_marks.xlsx"


def artifact_class_report_tex_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.tex"


def artifact_class_report_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.pdf"


def artifact_class_report_combined_landscape_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report_combined_landscape.pdf"


def artifact_class_report_combined_portrait_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report_combined_portrait.pdf"


def artifact_class_report_combined_landscape_with_questions_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report_combined_landscape_with_questions.pdf"


def artifact_class_report_combined_portrait_list_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report_combined_portrait_list.pdf"


def artifact_class_report_pdf_2up_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report_2up.pdf"


def artifact_class_report_combined_portrait_2up_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report_combined_portrait_2up.pdf"


def artifact_class_grade_histogram_raw_path(artifact_dir: Path) -> Path:
    """Raw-percentage grade-distribution histogram PNG"""
    return artifact_class_report_dir(artifact_dir) / "grade_histogram_raw.png"


def artifact_class_grade_histogram_curved_path(artifact_dir: Path) -> Path:
    """Curved-percentage grade-distribution histogram PNG"""
    return artifact_class_report_dir(artifact_dir) / "grade_histogram_curved.png"


def artifact_class_question_difficulty_path(artifact_dir: Path) -> Path:
    """Per-question difficulty bar chart PNG embedded in the class report"""
    return artifact_class_report_dir(artifact_dir) / "question_difficulty.png"


# ---------------------------------------------------------------------------
# Review queue (side-channel artifact for human spot-check)
# ---------------------------------------------------------------------------

def artifact_review_queue_json_path(artifact_dir: Path) -> Path:
    """Side-channel review queue (medium / low confidence marks).

    Pure side artifact — never loaded by any pipeline step; intended for manual
    spot-checking by the human marker.
    """
    return artifact_dir / REVIEW_QUEUE_DIR / "review.json"


def artifact_review_queue_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / REVIEW_QUEUE_DIR / "review.md"


# ---------------------------------------------------------------------------
# Timing summary (timing only, no accuracy/cost)
# ---------------------------------------------------------------------------

def artifact_timing_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / TIMING_DIR / "timing.json"


def artifact_timing_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / TIMING_DIR / "timing.md"


# ---------------------------------------------------------------------------
# Accuracy evaluation (only when ground truth present)
# ---------------------------------------------------------------------------

def artifact_accuracy_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / ACCURACY_DIR / "accuracy.json"


# ---------------------------------------------------------------------------
# AI costs
# ---------------------------------------------------------------------------

def artifact_cost_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / AI_COSTS_DIR / "cost.json"


def artifact_cost_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / AI_COSTS_DIR / "cost.md"


# ---------------------------------------------------------------------------
# Scaffold prompt path (for ai_scaffold.py and scaffold_gemini.py)
# ---------------------------------------------------------------------------

def artifact_scaffold_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Prompt file for scaffold AI calls (layout, exam questions, mark scheme, graphics).

    Routes by content of *name* to the appropriate step folder.
    Use content-only names (no step-number prefix), e.g. ``"exam_questions"``,
    ``"mark_scheme_p1"``, ``"detect_layout"``, ``"mark_scheme_graphics_detect_p1"``,
    ``"assign_scheme_questions_p1"``.
    """
    # Order matters: check most-specific first.
    if "assign_scheme_questions" in name:
        return artifact_dir / ASSIGN_QUESTIONS_DIR / f"{name}_prompt.md"
    if "mark_scheme" in name and "graphics" in name:
        return artifact_dir / SCHEME_GRAPHICS_DIR / f"{name}_prompt.md"
    if "mark_scheme" in name:
        return artifact_dir / PARSE_SCHEME_DIR / f"{name}_prompt.md"
    if "exam_scaffold" in name:
        return artifact_dir / DETECT_SCAFFOLD_DIR / f"{name}_prompt.md"
    if "detect_layout" in name or "layout" in name:
        return artifact_dir / LAYOUT_DIR / f"{name}_prompt.md"
    # Catch-all: per-page exam-questions fill prompts (Phase B).
    return artifact_dir / FILL_SCAFFOLD_DIR / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Deprecated generic prompt router (kept for callers not yet migrated)
# ---------------------------------------------------------------------------

def artifact_prompt_path(artifact_dir: Path, name: str) -> Path:
    """DEPRECATED — use the specific prompt path functions instead.

    Routing:
    - ``10_cover_p*`` → step-10 cover-scan dir
    - ``11_name*``    → step-11 names subdir
    - ``13_*``        → step-13 page-order root
    - ``14_*``        → step-14 blank-pages root
    - ``8_*``         → step-8 geometry root
    - ``3_*``         → step-3 root
    - ``20_*``        → marking students dir (legacy pre-renumber)
    - everything else → ``artifact_scaffold_prompt_path``
    """
    if name.startswith("10_cover_p"):
        return artifact_cover_scan_prompt_path(artifact_dir, name)
    if name.startswith("11_name"):
        return artifact_names_prompt_path(artifact_dir, name)
    if name.startswith("13_"):
        return artifact_dir / PAGE_ORDER_DIR / f"{name}_prompt.md"
    if name.startswith("14_"):
        return artifact_dir / EXAM_BLANK_DIR / f"{name}_prompt.md"
    if name.startswith("8_"):
        return artifact_geometry_prompt_path(artifact_dir, name)
    if name.startswith("3_"):
        return artifact_dir / STUDENT_LIST_DIR / f"{name}_prompt.md"
    if name.startswith("20_"):
        return artifact_marking_students_dir(artifact_dir) / f"{name}_prompt.md"
    return artifact_scaffold_prompt_path(artifact_dir, name)


# ---------------------------------------------------------------------------
# Extract-answers tool (independent sub-pipeline)
# ---------------------------------------------------------------------------

def extract_answers_output_dir(
    pdf_stem: str, output_base: str | Path = "output"
) -> Path:
    """Directory for one ``extract_answers`` run: ``output/extract_answers/<safe_stem>/``."""
    return Path(output_base) / "extract_answers" / safe_path_stem(pdf_stem)


# ---------------------------------------------------------------------------
# Scaffold cache lookup (with legacy path fallbacks for pre-restructure runs)
# ---------------------------------------------------------------------------

def find_scaffold_cache_file(
    exam_folder: Path, output_base: str | Path = "output/xscore"
) -> Path | None:
    """First existing scaffold cache: new step-folder layout first, then legacy locations.

    Checks new per-step folder paths first, then pre-restructure root-level paths,
    then the legacy ``output/<stem>/`` tree for very old runs.
    """
    for base in (output_base, "output"):
        ad = exam_artifact_dir(exam_folder, base)
        for p in (
            artifact_scaffold_xml_path(ad),                   # CREATE_REPORT_DIR/report.xml (currently 25_)
            artifact_scaffold_json_path(ad),                  # CREATE_REPORT_DIR/report.json (currently 25_)
            ad / "24_create_report" / "report.xml",           # post-detect-subject legacy
            ad / "24_create_report" / "report.json",          # post-detect-subject legacy
            ad / "23_create_report" / "report.xml",           # post-extract-student-answers legacy
            ad / "23_create_report" / "report.json",          # post-extract-student-answers legacy
            ad / "22_create_report" / "report.xml",           # post-assign-scheme-questions legacy
            ad / "22_create_report" / "report.json",          # post-assign-scheme-questions legacy
            ad / "21_create_report" / "report.xml",
            ad / "21_create_report" / "report.json",
            ad / "20_create_report" / "report.xml",
            ad / "20_create_report" / "report.json",
            ad / "19_create_report" / "report.xml",
            ad / "19_create_report" / "report.json",
            ad / "18_create_report" / "report.xml",
            ad / "18_create_report" / "report.json",
            ad / "17_create_report" / "report.xml",
            ad / "17_create_report" / "report.json",
            ad / "16_create_report" / "report.xml",
            ad / "16_create_report" / "report.json",
            ad / "12_report.xml",                             # pre-restructure legacy
            ad / "12_report.json",                            # pre-restructure legacy
            ad / "exam" / "12_report.json",                   # older legacy (pre-2025)
            ad / "scaffold" / "12_report.json",               # older legacy
            ad / "6_report.json",                             # oldest legacy (2024)
        ):
            if p.is_file():
                return p
    for p in (
        exam_folder / "scaffolds" / "scaffold_cache.json",
        exam_folder / "scaffold_cache.json",
    ):
        if p.is_file():
            return p
    return None


def is_completed_run(run_dir: Path) -> bool:
    """True iff *run_dir* contains a finished scaffold report from any era.

    Probes the live per-step layout first, then the pre-restructure flat
    layout. Used by ``--from-step`` to filter "valid prior runs" without
    each caller maintaining its own legacy probe list.
    """
    candidates = (
        artifact_scaffold_xml_path(run_dir),           # current — derived from CREATE_REPORT_DIR
        run_dir / "24_create_report" / "report.xml",   # post-detect-subject legacy
        run_dir / "23_create_report" / "report.xml",   # post-extract-student-answers legacy
        run_dir / "22_create_report" / "report.xml",   # post-assign-scheme-questions legacy
        run_dir / "21_create_report" / "report.xml",
        run_dir / "20_create_report" / "report.xml",
        run_dir / "19_create_report" / "report.xml",
        run_dir / "18_create_report" / "report.xml",
        run_dir / "17_create_report" / "report.xml",
        run_dir / "16_create_report" / "report.xml",
        run_dir / "12_report.json",                    # pre-restructure legacy
    )
    return any(p.exists() for p in candidates)
