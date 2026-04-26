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
    CLEANED_SCAN_PDF,
    STEP_01,
    STEP_03,
    STEP_05,
    STEP_06,
    STEP_07,
    STEP_08_COVER_EMPTY,
    STEP_09_COVER_SCAN,
    STEP_10_GEOMETRY,
    STEP_11_COVER_VERIFY,
    STEP_12_NAMES,
    STEP_13_PAGE_ORDER,
    STEP_14_EXAM_BLANK,
    STEP_15_HANDWRITING,
    STEP_16_LAYOUT,
    STEP_17_CUT,
    STEP_18_PARSE_EXAM,
    STEP_19_GRAPHICS,
    STEP_20_ASSIGN_QUESTIONS,
    STEP_21_PARSE_SCHEME,
    STEP_22_CREATE_REPORT,
    STEP_23_BLUEPRINTS,
    STEP_24_AI_MARKING,
    STEP_25_COMPILE_REPORTS,
    STEP_25_STUDENT_REPORTS,
    STEP_26_CLASS_STATS,
    STEP_27_STUDENT_PDFS,
    STEP_28_CLASS_REPORT,
    STEP_29_REVIEW_QUEUE,
    STEP_30_TIMING,
    STEP_31_ACCURACY,
    STEP_32_AI_COSTS,
    SUBDIR_INPUT,
    SUBDIR_NAMES,
    SUBDIR_STUDENTS,
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
# Input copies — step 2 (folder selection)
# ---------------------------------------------------------------------------

def artifact_input_dir(artifact_dir: Path) -> Path:
    """Directory that receives copies of all input files used by this run."""
    return artifact_dir / SUBDIR_INPUT


# ---------------------------------------------------------------------------
# Step 1 — Parse grading instructions
# ---------------------------------------------------------------------------

def artifact_parse_summary_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_01 / "summary.json"


def artifact_parse_prompt_path(artifact_dir: Path) -> Path:
    """Step 1: parse-instruction prompt JSON; the matching response file is
    written by ``save_response`` as ``parse_prompt_response.txt`` alongside it.
    """
    return artifact_dir / STEP_01 / "parse_prompt.json"


# ---------------------------------------------------------------------------
# Step 3 — Read student list
# ---------------------------------------------------------------------------

def artifact_students_json_path(artifact_dir: Path) -> Path:
    """Step 3: student roster as a JSON array of name strings."""
    return artifact_dir / STEP_03 / "students.json"


def artifact_students_markdown_path(artifact_dir: Path) -> Path:
    """Step 3: human-readable numbered student list."""
    return artifact_dir / STEP_03 / "students.md"


def artifact_student_list_prompt_path(artifact_dir: Path) -> Path:
    """Step 3: prompt file for student-list AI call."""
    return artifact_dir / STEP_03 / "student_list_prompt.md"


# ---------------------------------------------------------------------------
# Step 8 — Cover page detection (empty exam)
# ---------------------------------------------------------------------------

def artifact_cover_page_dir(artifact_dir: Path) -> Path:
    """Step 8: directory for empty-exam cover-page detection artifacts."""
    return artifact_dir / STEP_08_COVER_EMPTY


# ---------------------------------------------------------------------------
# Step 9 — Cover page detection (scan, first page only)
# ---------------------------------------------------------------------------

def artifact_cover_scan_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 9: prompt file for scan first-page cover detection."""
    return artifact_dir / STEP_09_COVER_SCAN / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Step 10 — Scan geometry (pages per student)
# ---------------------------------------------------------------------------

def artifact_geometry_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_10_GEOMETRY / "exam_geometry.json"


def artifact_geometry_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_10_GEOMETRY / "exam_geometry.md"


def artifact_geometry_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 10: prompt file for geometry AI calls."""
    return artifact_dir / STEP_10_GEOMETRY / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Step 11 — Cover page verification (remaining students)
# ---------------------------------------------------------------------------

def artifact_cover_verify_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 11: prompt file for per-position cover verification calls."""
    return artifact_dir / STEP_11_COVER_VERIFY / f"{name}_prompt.md"


def artifact_cover_verify_json_path(artifact_dir: Path) -> Path:
    """Step 11: per-position cover_ok dict persisted as JSON."""
    return artifact_dir / STEP_11_COVER_VERIFY / "cover_ok.json"


# ---------------------------------------------------------------------------
# Step 12 — Student names
# ---------------------------------------------------------------------------

def artifact_exam_student_list_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_12_NAMES / "exam_student_list.json"


def artifact_exam_student_list_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_12_NAMES / "exam_student_list.md"


def artifact_names_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 12: prompt file for name-detection AI calls (one per scan page)."""
    return artifact_dir / STEP_12_NAMES / "names" / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Step 13 — Page order
# ---------------------------------------------------------------------------

def artifact_page_order_txt_path(artifact_dir: Path, student: str) -> Path:
    """Step 13: per-student page-order detection text file."""
    return artifact_dir / STEP_13_PAGE_ORDER / f"page_order_{safe_student_name(student)}.txt"


def artifact_page_order_prompt_path(artifact_dir: Path, student: str) -> Path:
    """Step 13: per-student prompt file for page-order AI call."""
    return artifact_dir / STEP_13_PAGE_ORDER / f"page_order_{safe_student_name(student)}_prompt.md"


def artifact_page_order_empty_exam_txt_path(artifact_dir: Path) -> Path:
    """Step 13: empty-exam page-order detection text file."""
    return artifact_dir / STEP_13_PAGE_ORDER / "page_order_empty_exam.txt"


# ---------------------------------------------------------------------------
# Step 14 — Exam blank detection (text-only)
# ---------------------------------------------------------------------------

def artifact_exam_blank_json_path(artifact_dir: Path) -> Path:
    """Step 14: blank exam pages list JSON."""
    return artifact_dir / STEP_14_EXAM_BLANK / "blank_exam_pages.json"


def artifact_exam_blank_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 14: prompt file for exam blank-detection AI calls."""
    return artifact_dir / STEP_14_EXAM_BLANK / f"{name}_prompt.md"


def artifact_blank_detection_txt_path(artifact_dir: Path) -> Path:
    """Step 14: empty-exam blank-detection output text file."""
    return artifact_dir / STEP_14_EXAM_BLANK / "blank_detection_empty_exam.txt"


# ---------------------------------------------------------------------------
# Step 15 — Student handwriting check (vision)
# ---------------------------------------------------------------------------

def artifact_handwriting_json_path(artifact_dir: Path) -> Path:
    """Step 15: per-student handwriting detection results JSON."""
    return artifact_dir / STEP_15_HANDWRITING / "handwriting.json"


def artifact_handwriting_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 15: prompt file for handwriting AI calls."""
    return artifact_dir / STEP_15_HANDWRITING / f"{name}_prompt.md"


def artifact_handwriting_dir(artifact_dir: Path) -> Path:
    """Step 15: directory of JPEG images rendered for handwriting checks."""
    return artifact_dir / STEP_15_HANDWRITING / "scan_pages"


# ---------------------------------------------------------------------------
# Step 16 — Detect exam layout
# ---------------------------------------------------------------------------

def artifact_exam_layout_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_16_LAYOUT / "exam_layout.json"


def artifact_exam_layout_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_16_LAYOUT / "exam_layout.md"


def artifact_exam_layout_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_16_LAYOUT / "exam_layout.xml"


def artifact_exam_layout_raw_path(artifact_dir: Path, fmt: str = "json") -> Path:
    """Step 16: raw AI response before parsing (layout detection)."""
    return artifact_dir / STEP_16_LAYOUT / f"exam_layout_raw.{fmt}"


# ---------------------------------------------------------------------------
# Step 17 — Cut exam PDF (split multi-up layout into single logical pages)
# ---------------------------------------------------------------------------

def artifact_split_exam_pdf_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_17_CUT / "split_exam.pdf"


# ---------------------------------------------------------------------------
# Step 18 — Parse exam PDF
# ---------------------------------------------------------------------------

def artifact_exam_input_pdf_path(artifact_dir: Path) -> Path:
    """Copy of the original exam PDF uploaded to Gemini (1×1 mode)."""
    return artifact_dir / STEP_18_PARSE_EXAM / "exam_input.pdf"


def artifact_exam_questions_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_18_PARSE_EXAM / "exam_questions.json"


def artifact_exam_questions_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_18_PARSE_EXAM / "exam_questions.md"


def artifact_exam_questions_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_18_PARSE_EXAM / "exam_questions.xml"


def artifact_exam_questions_raw_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_18_PARSE_EXAM / "exam_questions_raw.xml"


def artifact_exam_questions_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / STEP_18_PARSE_EXAM / f"exam_questions.{fmt}"


def artifact_exam_questions_raw_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / STEP_18_PARSE_EXAM / f"exam_questions_raw.{fmt}"


# ---------------------------------------------------------------------------
# Step 19 — Detect mark scheme graphics (per-page splits + graphics detection)
# ---------------------------------------------------------------------------

def artifact_mark_scheme_pages_dir(artifact_dir: Path) -> Path:
    """Per-page PDFs (one per mark scheme page) — produced by step 19, consumed by step 20 + 21."""
    return artifact_dir / STEP_19_GRAPHICS / "pages"


def artifact_mark_scheme_graphics_dir(artifact_dir: Path) -> Path:
    """Directory of images extracted from the mark scheme."""
    return artifact_dir / STEP_19_GRAPHICS / "graphics"


def artifact_mark_scheme_graphics_json_path(artifact_dir: Path) -> Path:
    """Step 19: detected graphics positions per question."""
    return artifact_dir / STEP_19_GRAPHICS / "mark_scheme_graphics.json"


# ---------------------------------------------------------------------------
# Step 20 — Assign questions to mark scheme pages
# ---------------------------------------------------------------------------

def artifact_questions_per_page_path(artifact_dir: Path) -> Path:
    """Step 20: ``{page_num: [question_numbers]}`` JSON used by step 21 to filter
    its per-page scaffold to only the relevant questions."""
    return artifact_dir / STEP_20_ASSIGN_QUESTIONS / "questions_per_page.json"


# ---------------------------------------------------------------------------
# Step 21 — Parse mark scheme
# ---------------------------------------------------------------------------

def artifact_mark_scheme_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_21_PARSE_SCHEME / "mark_scheme.json"


def artifact_mark_scheme_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_21_PARSE_SCHEME / "mark_scheme.md"


def artifact_mark_scheme_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_21_PARSE_SCHEME / "mark_scheme.xml"


def artifact_mark_scheme_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / STEP_21_PARSE_SCHEME / f"mark_scheme.{fmt}"


# ---------------------------------------------------------------------------
# Step 22 — Create report / scaffold cache
# ---------------------------------------------------------------------------

def artifact_scaffold_xml_path(artifact_dir: Path) -> Path:
    """Merged exam + mark scheme XML scaffold cache."""
    return artifact_dir / STEP_22_CREATE_REPORT / "report.xml"


def artifact_scaffold_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_22_CREATE_REPORT / "report.json"


def artifact_scaffold_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_22_CREATE_REPORT / "report.md"


def artifact_short_scaffold_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_22_CREATE_REPORT / "short_report.json"


def artifact_short_scaffold_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_22_CREATE_REPORT / "short_report.md"


# ---------------------------------------------------------------------------
# Step 23 — AI marking blueprints
# ---------------------------------------------------------------------------

def artifact_blueprint_path(artifact_dir: Path, page: int, fmt: str = "yaml") -> Path:
    return artifact_dir / STEP_23_BLUEPRINTS / f"blueprint_page_{page}.{fmt}"


def artifact_blueprint_xml_path(artifact_dir: Path, page: int) -> Path:
    return artifact_dir / STEP_23_BLUEPRINTS / f"blueprint_page_{page}.xml"


def artifact_blueprint_json_path(artifact_dir: Path, page: int) -> Path:
    return artifact_dir / STEP_23_BLUEPRINTS / f"blueprint_page_{page}.json"


def artifact_blueprint_md_path(artifact_dir: Path, page: int) -> Path:
    return artifact_dir / STEP_23_BLUEPRINTS / f"blueprint_page_{page}.md"


# ---------------------------------------------------------------------------
# Step 24 — AI marking
# ---------------------------------------------------------------------------

def artifact_marking_students_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student marking files."""
    return artifact_dir / STEP_24_AI_MARKING / "students"


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
# Step 25 — Per-student reports (XML + MD)
# ---------------------------------------------------------------------------

def artifact_student_reports_dir(artifact_dir: Path) -> Path:
    """Parent directory holding per-student report subfolders (step 25)."""
    return artifact_dir / STEP_25_STUDENT_REPORTS


def artifact_student_report_dir(artifact_dir: Path, student: str) -> Path:
    """Per-student subfolder for XML + Markdown reports (step 25)."""
    return artifact_student_reports_dir(artifact_dir) / safe_student_name(student)


def artifact_student_report_xml_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_report_dir(artifact_dir, student) / f"{safe_student_name(student)}.xml"


def artifact_student_report_md_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_report_dir(artifact_dir, student) / f"{safe_student_name(student)}.md"


# Backward-compat alias for callers that haven't migrated to the new name.
artifact_reports_students_dir = artifact_student_reports_dir


# ---------------------------------------------------------------------------
# Step 26 — Class statistics + grade curve
# ---------------------------------------------------------------------------

def artifact_class_stats_json_path(artifact_dir: Path) -> Path:
    """Step 26: class average + curve offset, written before per-student PDFs."""
    return artifact_dir / STEP_26_CLASS_STATS / "class_stats.json"


# ---------------------------------------------------------------------------
# Step 27 — Per-student PDFs (TeX + xelatex output)
# ---------------------------------------------------------------------------

def artifact_student_pdfs_dir(artifact_dir: Path) -> Path:
    """Parent directory holding per-student PDF subfolders (step 27)."""
    return artifact_dir / STEP_27_STUDENT_PDFS


def artifact_student_pdf_dir(artifact_dir: Path, student: str) -> Path:
    """Per-student subfolder for .tex + .pdf files (step 27)."""
    return artifact_student_pdfs_dir(artifact_dir) / safe_student_name(student)


def artifact_student_report_tex_landscape_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_dir(artifact_dir, student) / f"{safe_student_name(student)}_landscape.tex"


def artifact_student_report_pdf_landscape_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_dir(artifact_dir, student) / f"{safe_student_name(student)}_landscape.pdf"


def artifact_student_report_tex_portrait_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_dir(artifact_dir, student) / f"{safe_student_name(student)}_portrait.tex"


def artifact_student_report_pdf_portrait_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_dir(artifact_dir, student) / f"{safe_student_name(student)}_portrait.pdf"


def artifact_student_report_pdf_portrait_2up_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_dir(artifact_dir, student) / f"{safe_student_name(student)}_portrait_2up.pdf"


def artifact_student_report_tex_portrait_large_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_dir(artifact_dir, student) / f"{safe_student_name(student)}_portrait_large.tex"


def artifact_student_report_pdf_portrait_large_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdf_dir(artifact_dir, student) / f"{safe_student_name(student)}_portrait_large.pdf"


# ---------------------------------------------------------------------------
# Step 28 — Class report (XML/MD/TeX/PDF + combined PDF)
# ---------------------------------------------------------------------------

def artifact_class_report_dir(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_28_CLASS_REPORT


def artifact_class_report_xml_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.xml"


def artifact_class_report_md_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.md"


def artifact_class_report_tex_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.tex"


def artifact_class_report_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.pdf"


def artifact_class_report_combined_landscape_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report_combined_landscape.pdf"


def artifact_class_report_combined_portrait_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report_combined_portrait.pdf"


def artifact_class_report_pdf_2up_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report_2up.pdf"


def artifact_class_report_combined_portrait_2up_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report_combined_portrait_2up.pdf"


def artifact_class_grade_histogram_path(artifact_dir: Path) -> Path:
    """Grade-distribution histogram PNG embedded in the class report (step 28)."""
    return artifact_class_report_dir(artifact_dir) / "grade_histogram.png"


def artifact_class_question_difficulty_path(artifact_dir: Path) -> Path:
    """Per-question difficulty bar chart PNG embedded in the class report (step 28)."""
    return artifact_class_report_dir(artifact_dir) / "question_difficulty.png"


# ---------------------------------------------------------------------------
# Step 29 — Review queue (side-channel artifact for human spot-check)
# ---------------------------------------------------------------------------

def artifact_review_queue_json_path(artifact_dir: Path) -> Path:
    """Side-channel review queue (medium / low confidence marks).

    Pure side artifact — never loaded by any pipeline step; intended for manual
    spot-checking by the human marker.
    """
    return artifact_dir / STEP_29_REVIEW_QUEUE / "review.json"


def artifact_review_queue_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_29_REVIEW_QUEUE / "review.md"


# ---------------------------------------------------------------------------
# Step 30 — Timing summary (timing only, no accuracy/cost)
# ---------------------------------------------------------------------------

def artifact_timing_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_30_TIMING / "timing.json"


def artifact_timing_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_30_TIMING / "timing.md"


# ---------------------------------------------------------------------------
# Step 31 — Accuracy evaluation (only when ground truth present)
# ---------------------------------------------------------------------------

def artifact_accuracy_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_31_ACCURACY / "accuracy.json"


# ---------------------------------------------------------------------------
# Step 32 — AI costs
# ---------------------------------------------------------------------------

def artifact_cost_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_32_AI_COSTS / "cost.json"


def artifact_cost_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_32_AI_COSTS / "cost.md"


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
        return artifact_dir / STEP_20_ASSIGN_QUESTIONS / f"{name}_prompt.md"
    if "mark_scheme" in name and "graphics" in name:
        return artifact_dir / STEP_19_GRAPHICS / f"{name}_prompt.md"
    if "mark_scheme" in name:
        return artifact_dir / STEP_21_PARSE_SCHEME / f"{name}_prompt.md"
    if "detect" in name or "layout" in name:
        return artifact_dir / STEP_16_LAYOUT / f"{name}_prompt.md"
    return artifact_dir / STEP_18_PARSE_EXAM / f"{name}_prompt.md"


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
    - ``20_*``        → marking students dir (legacy — pre-renumber, when marking lived at step 20)
    - everything else → ``artifact_scaffold_prompt_path``
    """
    if name.startswith("10_cover_p"):
        return artifact_cover_scan_prompt_path(artifact_dir, name)
    if name.startswith("11_name"):
        return artifact_names_prompt_path(artifact_dir, name)
    if name.startswith("13_"):
        return artifact_dir / STEP_13_PAGE_ORDER / f"{name}_prompt.md"
    if name.startswith("14_"):
        return artifact_dir / STEP_14_EXAM_BLANK / f"{name}_prompt.md"
    if name.startswith("8_"):
        return artifact_geometry_prompt_path(artifact_dir, name)
    if name.startswith("3_"):
        return artifact_dir / STEP_03 / f"{name}_prompt.md"
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
            artifact_scaffold_xml_path(ad),                   # 22_create_report/report.xml
            artifact_scaffold_json_path(ad),                  # 22_create_report/report.json
            ad / "21_create_report" / "report.xml",           # post-assign-scheme-questions split legacy
            ad / "21_create_report" / "report.json",          # post-assign-scheme-questions split legacy
            ad / "20_create_report" / "report.xml",           # post-step-14-split legacy
            ad / "20_create_report" / "report.json",          # post-step-14-split legacy
            ad / "19_create_report" / "report.xml",           # post-step-18-split legacy
            ad / "19_create_report" / "report.json",          # post-step-18-split legacy
            ad / "18_create_report" / "report.xml",           # post-step-split-refactor legacy
            ad / "18_create_report" / "report.json",          # post-step-split-refactor legacy
            ad / "17_create_report" / "report.xml",           # post-step-16-refactor legacy
            ad / "17_create_report" / "report.json",          # post-step-16-refactor legacy
            ad / "16_create_report" / "report.xml",           # pre-step-16-refactor legacy
            ad / "16_create_report" / "report.json",          # pre-step-16-refactor legacy
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


def is_cs_exam(*pdf_paths: "Path | None") -> bool:
    """Whether the exam involves code/pseudocode (gates code-formatting prompt rules).

    Heuristic — temporary: True if any provided PDF filename contains "0478"
    (Cambridge IGCSE Computer Science). To be replaced by proper subject
    detection later; keep call sites going through this function so the
    eventual upgrade is a one-file change.
    """
    return any("0478" in p.name for p in pdf_paths if p is not None)


def is_completed_run(run_dir: Path) -> bool:
    """True iff *run_dir* contains a finished scaffold report from any era.

    Probes the live per-step layout first, then the pre-restructure flat
    layout. Used by ``--from-step`` to filter "valid prior runs" without
    each caller maintaining its own legacy probe list.
    """
    candidates = (
        run_dir / "22_create_report" / "report.xml",   # current
        run_dir / "21_create_report" / "report.xml",   # post-assign-scheme-questions legacy
        run_dir / "20_create_report" / "report.xml",   # post-step-14-split legacy
        run_dir / "19_create_report" / "report.xml",   # post-step-18-split legacy
        run_dir / "18_create_report" / "report.xml",   # post-step-split legacy
        run_dir / "17_create_report" / "report.xml",   # post-step-16 refactor legacy
        run_dir / "16_create_report" / "report.xml",   # pre-step-16 refactor legacy
        run_dir / "12_report.json",                    # pre-restructure legacy
    )
    return any(p.exists() for p in candidates)
