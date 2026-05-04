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
    DETECT_SUBJECT_DIR,
    EMPTY_EXAM_CLASSIFY_DIR,
    EXTRACT_ANSWERS_DIR,
    EXTRACT_QUESTION_NUMBERS_DIR,
    EXTRACT_QUESTIONS_DIR,
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
    TRANSCRIBE_SCHEME_GRAPHICS_DIR,
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
    """Parse-instruction prompt; the matching response file is written by
    ``save_response`` as ``parse_response.txt`` alongside it (the
    ``_prompt`` suffix is stripped from the stem before joining).
    """
    return artifact_dir / PARSE_INSTRUCTIONS_DIR / "parse_prompt.txt"


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
    return artifact_dir / STUDENT_LIST_DIR / "student_list_prompt.txt"


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
    return artifact_dir / COVER_SCAN_DIR / f"{name}_prompt.txt"


# ---------------------------------------------------------------------------
# Scan geometry (pages per student)
# ---------------------------------------------------------------------------

def artifact_geometry_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / GEOMETRY_DIR / "exam_geometry.json"


def artifact_geometry_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / GEOMETRY_DIR / "exam_geometry.md"


def artifact_geometry_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Prompt file for geometry AI calls."""
    return artifact_dir / GEOMETRY_DIR / f"{name}_prompt.txt"


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
    return artifact_dir / DETECT_SUBJECT_DIR / f"{name}_prompt.txt"


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
    return artifact_dir / STUDENT_NAMES_DIR / "names" / f"{name}_prompt.txt"


# ---------------------------------------------------------------------------
# Page order
# ---------------------------------------------------------------------------

def artifact_page_order_txt_path(artifact_dir: Path, student: str) -> Path:
    """Per-student page-order detection text file."""
    return artifact_dir / PAGE_ORDER_DIR / f"page_order_{safe_student_name(student)}.txt"


def artifact_page_order_empty_exam_txt_path(artifact_dir: Path) -> Path:
    """Empty-exam page-order detection text file."""
    return artifact_dir / PAGE_ORDER_DIR / "page_order_empty_exam.txt"


def artifact_page_order_issues_path(artifact_dir: Path) -> Path:
    """Structured page-order check result (status + per-page issues)."""
    return artifact_dir / PAGE_ORDER_DIR / "issues.json"


# ---------------------------------------------------------------------------
# Empty-exam page classification (step 14, vision)
# ---------------------------------------------------------------------------

def artifact_empty_exam_classifications_json_path(artifact_dir: Path) -> Path:
    """Per-empty-exam-page classifications (page_type + page_number) JSON."""
    return artifact_dir / EMPTY_EXAM_CLASSIFY_DIR / "empty_exam_classifications.json"


def artifact_empty_exam_pages_dir(artifact_dir: Path) -> Path:
    """Directory of per-page artifacts for the empty-exam classifier (step 14).

    Files are PDFs on the Gemini path (one-page slices) and JPEGs on the
    rasterized fallback. Prompt sidecars land here too.
    """
    return artifact_dir / EMPTY_EXAM_CLASSIFY_DIR / "empty_exam_pages"


# ---------------------------------------------------------------------------
# Student handwriting check (step 15, vision per scan page)
# ---------------------------------------------------------------------------

def artifact_handwriting_json_path(artifact_dir: Path) -> Path:
    """Per-student handwriting detection results JSON."""
    return artifact_dir / HANDWRITING_DIR / "handwriting.json"


def artifact_handwriting_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Prompt file for handwriting AI calls."""
    return artifact_dir / HANDWRITING_DIR / f"{name}_prompt.txt"


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


def artifact_continuation_refs_json_path(artifact_dir: Path) -> Path:
    """Diagnostic listing each blank/writing-space page attached as continuation."""
    return artifact_dir / CROSS_PAGE_CONTEXT_DIR / "continuation_refs.json"


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
# Extract question numbers from empty exam (step 19)
# ---------------------------------------------------------------------------

def artifact_exam_scaffold_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    """Intermediate scaffold — number/type/page/subpage/marks, no text."""
    return artifact_dir / EXTRACT_QUESTION_NUMBERS_DIR / f"exam_scaffold.{fmt}"


def artifact_exam_scaffold_raw_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / EXTRACT_QUESTION_NUMBERS_DIR / f"exam_scaffold_raw.{fmt}"


# ---------------------------------------------------------------------------
# Extract questions from empty exam (step 20 — text + options per question)
# ---------------------------------------------------------------------------

def artifact_exam_questions_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / EXTRACT_QUESTIONS_DIR / "exam_questions.json"


def artifact_exam_questions_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / EXTRACT_QUESTIONS_DIR / "exam_questions.md"


def artifact_exam_questions_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / EXTRACT_QUESTIONS_DIR / "exam_questions.xml"


def artifact_exam_questions_raw_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / EXTRACT_QUESTIONS_DIR / "exam_questions_raw.xml"


def artifact_exam_questions_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / EXTRACT_QUESTIONS_DIR / f"exam_questions.{fmt}"


def artifact_exam_questions_raw_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / EXTRACT_QUESTIONS_DIR / f"exam_questions_raw.{fmt}"


def artifact_exam_pages_dir(artifact_dir: Path) -> Path:
    """Per-page PDFs from the post-cut exam PDF — produced and consumed by step 20 (extract_exam_questions)."""
    return artifact_dir / EXTRACT_QUESTIONS_DIR / "pages"


# ---------------------------------------------------------------------------
# Detect mark scheme graphics (per-page splits + graphics detection)
# ---------------------------------------------------------------------------

def artifact_mark_scheme_pages_dir(artifact_dir: Path) -> Path:
    """Per-page PDFs (one per mark scheme page) — produced by detect_mark_scheme_graphics, consumed by assign_scheme_questions and parse_mark_scheme."""
    return artifact_dir / SCHEME_GRAPHICS_DIR / "pages"


def artifact_mark_scheme_graphics_dir(artifact_dir: Path) -> Path:
    """Directory of images extracted from the mark scheme."""
    return artifact_dir / SCHEME_GRAPHICS_DIR / "graphics"


def artifact_mark_scheme_graphics_yaml_path(artifact_dir: Path) -> Path:
    """Detected graphics positions per question."""
    return artifact_dir / SCHEME_GRAPHICS_DIR / "mark_scheme_graphics.yaml"


# ---------------------------------------------------------------------------
# Assign questions to mark scheme pages
# ---------------------------------------------------------------------------

def artifact_questions_per_page_path(artifact_dir: Path) -> Path:
    """``{page_num: [question_numbers]}`` YAML used by parse_mark_scheme to filter
    its per-page scaffold to only the relevant questions."""
    return artifact_dir / ASSIGN_QUESTIONS_DIR / "questions_per_page.yaml"


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
# Transcribe mark scheme graphics
# ---------------------------------------------------------------------------

def artifact_scheme_graphic_transcriptions_path(artifact_dir: Path) -> Path:
    """Per-graphic textual descriptions consumed by step 29 (ai_marking)."""
    return artifact_dir / TRANSCRIBE_SCHEME_GRAPHICS_DIR / "transcriptions.yaml"


# ---------------------------------------------------------------------------
# Create report / scaffold cache
# ---------------------------------------------------------------------------

def artifact_scaffold_yaml_path(artifact_dir: Path) -> Path:
    """Merged exam + mark scheme YAML scaffold cache (primary format)."""
    return artifact_dir / CREATE_REPORT_DIR / "report.yaml"


def artifact_scaffold_xml_path(artifact_dir: Path) -> Path:
    """Legacy XML scaffold cache — kept for resume compatibility with old runs."""
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
    """Directory containing per-student extracted-answer subfolders."""
    return artifact_dir / EXTRACT_ANSWERS_DIR / "students"


def _student_answers_subdir(artifact_dir: Path, student: str) -> Path:
    return artifact_student_answers_dir(artifact_dir) / safe_student_name(student)


def artifact_student_answers_path(
    artifact_dir: Path, student: str, page: int, *, fmt: str = "yaml",
) -> Path:
    """Per-(student, page) extracted student answers. *fmt* is retained for
    back-compat but defaults to ``"yaml"`` since xml/json variants were
    removed."""
    return _student_answers_subdir(artifact_dir, student) / f"page_{page}.{fmt}"


def artifact_student_answers_prompt_path(
    artifact_dir: Path, student: str, page: int
) -> Path:
    """Prompt file saved alongside the extraction result for one (student, page)."""
    return _student_answers_subdir(artifact_dir, student) / f"page_{page}_prompt.txt"


def artifact_student_answers_failed_path(
    artifact_dir: Path, student: str, page: int
) -> Path:
    """Failure record when all extraction attempts are exhausted for a (student, page)."""
    return _student_answers_subdir(artifact_dir, student) / f"failed_page_{page}.json"


# ---------------------------------------------------------------------------
# AI marking
# ---------------------------------------------------------------------------

def artifact_marking_students_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student marking subfolders."""
    return artifact_dir / AI_MARKING_DIR / "students"


def artifact_mcq_corrections_path(artifact_dir: Path) -> Path:
    """29_ai_marking/mcq_corrections.yaml — audit log of MCQ corrections
    applied during marking based on the AI's corrected_student_answer field.
    Regenerated each step-29 run; an empty list when no corrections occurred."""
    return artifact_dir / AI_MARKING_DIR / "mcq_corrections.yaml"


def _marking_student_subdir(artifact_dir: Path, student: str) -> Path:
    return artifact_marking_students_dir(artifact_dir) / safe_student_name(student)


def artifact_marked_path(artifact_dir: Path, student: str, page: int, fmt: str = "yaml") -> Path:
    return _marking_student_subdir(artifact_dir, student) / f"page_{page}.{fmt}"


def artifact_marked_md_path(artifact_dir: Path, student: str, page: int) -> Path:
    return _marking_student_subdir(artifact_dir, student) / f"page_{page}.md"


def artifact_marked_failed_path(artifact_dir: Path, student: str, page: int) -> Path:
    """Failure record when all marking attempts are exhausted for a page."""
    return _marking_student_subdir(artifact_dir, student) / f"failed_page_{page}.json"


def artifact_marking_prompt_path(artifact_dir: Path, student: str, page: int) -> Path:
    """Prompt file saved alongside the marking result for one student page."""
    return _marking_student_subdir(artifact_dir, student) / f"page_{page}_prompt.txt"


# ---------------------------------------------------------------------------
# Per-student reports (XML + MD)
# ---------------------------------------------------------------------------

def artifact_student_reports_dir(artifact_dir: Path) -> Path:
    """Parent directory holding per-student report subfolders"""
    return artifact_dir / STUDENT_REPORTS_DIR


def artifact_student_report_dir(artifact_dir: Path, student: str) -> Path:
    """Per-student subfolder for YAML + Markdown reports"""
    return artifact_student_reports_dir(artifact_dir) / safe_student_name(student)


def artifact_student_report_yaml_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_report_dir(artifact_dir, student) / f"{safe_student_name(student)}.yaml"


def artifact_student_report_md_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_report_dir(artifact_dir, student) / f"{safe_student_name(student)}.md"


def artifact_student_report_md_attempted_path(artifact_dir: Path, student: str) -> Path:
    """`_attempted` sibling of the per-student Markdown report — same dir,
    `_attempted` suffix on the stem."""
    return artifact_student_report_dir(artifact_dir, student) / f"{safe_student_name(student)}_attempted.md"


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

# `_attempted` siblings: per-student reports filtered to only the questions
# the student wrote a non-empty answer for. Filenames carry the same
# `_attempted` suffix on the stem so a directory listing groups each pair.
_VARIANT_LANDSCAPE_ATTEMPTED                = "landscape_attempted"
_VARIANT_LANDSCAPE_WITH_QUESTIONS_ATTEMPTED = "landscape_with_questions_attempted"
_VARIANT_PORTRAIT_ATTEMPTED                 = "portrait_attempted"
_VARIANT_PORTRAIT_2UP_ATTEMPTED             = "portrait_2up_attempted"
_VARIANT_PORTRAIT_LARGE_ATTEMPTED           = "portrait_large_attempted"
_VARIANT_PORTRAIT_LIST_ATTEMPTED            = "portrait_list_attempted"


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


# `_attempted` per-student variants: same shape as above, with `_attempted`
# appended to both the variant subfolder and the file stem.
def artifact_student_report_tex_landscape_attempted_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_LANDSCAPE_ATTEMPTED) / f"{safe_student_name(student)}_landscape_attempted.tex"


def artifact_student_report_pdf_landscape_attempted_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_LANDSCAPE_ATTEMPTED) / f"{safe_student_name(student)}_landscape_attempted.pdf"


def artifact_student_report_tex_portrait_attempted_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_ATTEMPTED) / f"{safe_student_name(student)}_portrait_attempted.tex"


def artifact_student_report_pdf_portrait_attempted_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_ATTEMPTED) / f"{safe_student_name(student)}_portrait_attempted.pdf"


def artifact_student_report_pdf_portrait_2up_attempted_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_2UP_ATTEMPTED) / f"{safe_student_name(student)}_portrait_2up_attempted.pdf"


def artifact_student_report_tex_portrait_large_attempted_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_LARGE_ATTEMPTED) / f"{safe_student_name(student)}_portrait_large_attempted.tex"


def artifact_student_report_pdf_portrait_large_attempted_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_LARGE_ATTEMPTED) / f"{safe_student_name(student)}_portrait_large_attempted.pdf"


def artifact_student_report_tex_landscape_with_questions_attempted_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_LANDSCAPE_WITH_QUESTIONS_ATTEMPTED) / f"{safe_student_name(student)}_landscape_with_questions_attempted.tex"


def artifact_student_report_pdf_landscape_with_questions_attempted_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_LANDSCAPE_WITH_QUESTIONS_ATTEMPTED) / f"{safe_student_name(student)}_landscape_with_questions_attempted.pdf"


def artifact_student_report_tex_portrait_list_attempted_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_LIST_ATTEMPTED) / f"{safe_student_name(student)}_portrait_list_attempted.tex"


def artifact_student_report_pdf_portrait_list_attempted_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_variant_dir(artifact_dir, student, _VARIANT_PORTRAIT_LIST_ATTEMPTED) / f"{safe_student_name(student)}_portrait_list_attempted.pdf"


# ---------------------------------------------------------------------------
# Class report (XML/MD/TeX/PDF + combined PDF)
# ---------------------------------------------------------------------------

def artifact_class_report_dir(artifact_dir: Path) -> Path:
    return artifact_dir / CLASS_REPORT_DIR


def artifact_class_report_summary_dir(artifact_dir: Path) -> Path:
    """Class summary core (xml/md/tex/pdf/2up/aux/log + xlsx + charts/)."""
    return artifact_class_report_dir(artifact_dir) / "class_report"


def artifact_class_report_exam_questions_dir(artifact_dir: Path) -> Path:
    """Standalone exam-questions PDF + sources — one per run."""
    return artifact_class_report_dir(artifact_dir) / "exam_questions"


def artifact_exam_questions_tex_path(artifact_dir: Path) -> Path:
    """Standalone exam-questions PDF source — one per run."""
    return artifact_class_report_exam_questions_dir(artifact_dir) / "exam_questions.tex"


def artifact_exam_questions_pdf_path(artifact_dir: Path) -> Path:
    """Standalone exam-questions PDF — one per run."""
    return artifact_class_report_exam_questions_dir(artifact_dir) / "exam_questions.pdf"


def artifact_class_report_charts_dir(artifact_dir: Path) -> Path:
    """Embedded chart PNGs — nested under the summary folder."""
    return artifact_class_report_summary_dir(artifact_dir) / "charts"


def artifact_class_report_portrait_dir(artifact_dir: Path) -> Path:
    """Combined-with-students PDFs whose source pages are portrait."""
    return artifact_class_report_dir(artifact_dir) / "portrait"


def artifact_class_report_landscape_dir(artifact_dir: Path) -> Path:
    """Combined-with-students PDFs whose source pages are landscape."""
    return artifact_class_report_dir(artifact_dir) / "landscape"


def artifact_class_report_xml_path(artifact_dir: Path) -> Path:
    return artifact_class_report_summary_dir(artifact_dir) / "class_report.xml"


def artifact_class_report_md_path(artifact_dir: Path) -> Path:
    return artifact_class_report_summary_dir(artifact_dir) / "class_report.md"


def artifact_class_marks_xlsx_path(artifact_dir: Path) -> Path:
    """Machine-friendly per-student × per-question marks grid."""
    return artifact_class_report_dir(artifact_dir) / "class_marks.xlsx"


def artifact_class_report_tex_path(artifact_dir: Path) -> Path:
    return artifact_class_report_summary_dir(artifact_dir) / "class_report.tex"


def artifact_class_report_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_summary_dir(artifact_dir) / "class_report.pdf"


def artifact_class_report_combined_landscape_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_landscape_dir(artifact_dir) / "class_report_combined_landscape.pdf"


def artifact_class_report_combined_portrait_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_portrait_dir(artifact_dir) / "class_report_combined_portrait.pdf"


def artifact_class_report_combined_landscape_with_questions_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_landscape_dir(artifact_dir) / "class_report_combined_landscape_with_questions.pdf"


def artifact_class_report_combined_portrait_list_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_portrait_dir(artifact_dir) / "class_report_combined_portrait_list.pdf"


def artifact_class_report_pdf_2up_path(artifact_dir: Path) -> Path:
    return artifact_class_report_summary_dir(artifact_dir) / "class_report_2up.pdf"


def artifact_class_report_combined_portrait_2up_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_portrait_dir(artifact_dir) / "class_report_combined_portrait_2up.pdf"


def artifact_class_report_scheme_graphics_check_dir(artifact_dir: Path) -> Path:
    """Verification PDF showing each extracted scheme graphic next to its transcription."""
    return artifact_class_report_dir(artifact_dir) / "scheme_graphics_check"


def artifact_scheme_graphics_check_tex_path(artifact_dir: Path) -> Path:
    return artifact_class_report_scheme_graphics_check_dir(artifact_dir) / "scheme_graphics_check.tex"


def artifact_scheme_graphics_check_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_scheme_graphics_check_dir(artifact_dir) / "scheme_graphics_check.pdf"


def artifact_class_grade_histogram_raw_path(artifact_dir: Path) -> Path:
    """Raw-percentage grade-distribution histogram PNG"""
    return artifact_class_report_charts_dir(artifact_dir) / "grade_histogram_raw.png"


def artifact_class_grade_histogram_curved_path(artifact_dir: Path) -> Path:
    """Curved-percentage grade-distribution histogram PNG"""
    return artifact_class_report_charts_dir(artifact_dir) / "grade_histogram_curved.png"


def artifact_class_question_difficulty_path(artifact_dir: Path) -> Path:
    """Sub-question difficulty bar chart PNG (leaves) embedded in the class report"""
    return artifact_class_report_charts_dir(artifact_dir) / "question_difficulty.png"


def artifact_class_question_difficulty_top_path(artifact_dir: Path) -> Path:
    """Top-level-question difficulty bar chart PNG embedded in the class report"""
    return artifact_class_report_charts_dir(artifact_dir) / "question_difficulty_top.png"


# ---------------------------------------------------------------------------
# Review queue (side-channel artifact for human spot-check)
# ---------------------------------------------------------------------------

def artifact_review_queue_json_path(artifact_dir: Path) -> Path:
    """Side-channel review queue / confidence audit (every marked question).

    Pure side artifact — never loaded by any pipeline step; intended for manual
    spot-checking by the human marker.
    """
    return artifact_dir / REVIEW_QUEUE_DIR / "review.json"


def artifact_review_queue_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / REVIEW_QUEUE_DIR / "review.md"


def artifact_review_queue_txt_path(artifact_dir: Path) -> Path:
    """Plain-text mirror of the review queue — one line per marked question,
    ordered by ascending confidence. Same per-entry format the terminal echoes
    (top 10 only); the .txt holds the full sorted list for grep / less.
    """
    return artifact_dir / REVIEW_QUEUE_DIR / "review.txt"


# ---------------------------------------------------------------------------
# Timing summary (timing only, no accuracy/cost)
# ---------------------------------------------------------------------------

def artifact_timing_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / TIMING_DIR / "timing.json"


def artifact_timing_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / TIMING_DIR / "timing.md"


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
        return artifact_dir / ASSIGN_QUESTIONS_DIR / f"{name}_prompt.txt"
    if "mark_scheme" in name and "graphics" in name:
        return artifact_dir / SCHEME_GRAPHICS_DIR / f"{name}_prompt.txt"
    if "mark_scheme" in name:
        return artifact_dir / PARSE_SCHEME_DIR / f"{name}_prompt.txt"
    if "question_numbers" in name:
        return artifact_dir / EXTRACT_QUESTION_NUMBERS_DIR / f"{name}_prompt.txt"
    if "detect_layout" in name or "layout" in name:
        return artifact_dir / LAYOUT_DIR / f"{name}_prompt.txt"
    # Catch-all: per-page extract-exam-questions prompts (step 20).
    return artifact_dir / EXTRACT_QUESTIONS_DIR / f"{name}_prompt.txt"


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
            artifact_scaffold_yaml_path(ad),                  # CREATE_REPORT_DIR/report.yaml (primary)
            artifact_scaffold_xml_path(ad),                   # CREATE_REPORT_DIR/report.xml (legacy)
            artifact_scaffold_json_path(ad),                  # CREATE_REPORT_DIR/report.json (legacy)
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
        artifact_scaffold_yaml_path(run_dir),          # current primary — YAML
        artifact_scaffold_xml_path(run_dir),           # legacy XML
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
