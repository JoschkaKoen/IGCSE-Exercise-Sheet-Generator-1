"""Paths for per-exam derived artifacts (under ``output/xscore/<stem>/<timestamp>/``)."""

from __future__ import annotations

import re
from pathlib import Path

# Re-exported here for backwards compatibility; canonical location is find_exam_folder.py.
from xscore.marking.find_exam_folder import validate_input_files as validate_input_files  # noqa: F401


# ---------------------------------------------------------------------------
# Step-folder name constants
# ---------------------------------------------------------------------------

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
# Step 8 — Scan geometry
# ---------------------------------------------------------------------------

def artifact_geometry_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_08 / "exam_geometry.json"


def artifact_geometry_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_08 / "exam_geometry.md"


def artifact_geometry_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 8: prompt file for geometry AI calls."""
    return artifact_dir / STEP_08 / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Step 9 — Cover page detection
# ---------------------------------------------------------------------------

def artifact_cover_page_dir(artifact_dir: Path) -> Path:
    """Step 9: directory for empty-exam cover-page detection artifacts."""
    return artifact_dir / STEP_09_COVER


# ---------------------------------------------------------------------------
# Step 10 — Cover page detection (scan)
# ---------------------------------------------------------------------------

def artifact_cover_scan_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 10: prompt file for scan cover-page detection AI calls."""
    return artifact_dir / STEP_10_COVER_SCAN / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Step 11 — Student names
# ---------------------------------------------------------------------------

def artifact_exam_student_list_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_11_NAMES / "exam_student_list.json"


def artifact_exam_student_list_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_11_NAMES / "exam_student_list.md"


def artifact_names_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 11: prompt file for name-detection AI calls (one per scan page)."""
    return artifact_dir / STEP_11_NAMES / "names" / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Step 13 — Page order
# ---------------------------------------------------------------------------

def artifact_page_order_txt_path(artifact_dir: Path, student: str) -> Path:
    """Step 13: per-student page-order detection text file."""
    return artifact_dir / STEP_13_PAGE_ORDER / f"page_order_{safe_student_name(student)}.txt"


def artifact_page_order_prompt_path(artifact_dir: Path) -> Path:
    """Step 13: prompt file for page-order AI call."""
    return artifact_dir / STEP_13_PAGE_ORDER / "page_order_prompt.md"


def artifact_page_order_empty_exam_txt_path(artifact_dir: Path) -> Path:
    """Step 13: empty-exam page-order detection text file."""
    return artifact_dir / STEP_13_PAGE_ORDER / "page_order_empty_exam.txt"


# ---------------------------------------------------------------------------
# Step 14 — Blank pages
# ---------------------------------------------------------------------------

def artifact_blank_pages_json_path(artifact_dir: Path) -> Path:
    """Step 14: blank/handwriting detection results JSON."""
    return artifact_dir / STEP_14_BLANK_PAGES / "blank_pages.json"


def artifact_blank_pages_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 14: prompt file for blank-page/handwriting AI calls."""
    return artifact_dir / STEP_14_BLANK_PAGES / f"{name}_prompt.md"


def artifact_blank_pages_dir(artifact_dir: Path) -> Path:
    """Step 14: directory of JPEG images from blank-page detection."""
    return artifact_dir / STEP_14_BLANK_PAGES / "blank_pages"


def artifact_blank_detection_txt_path(artifact_dir: Path) -> Path:
    """Step 14: empty-exam blank-detection output text file."""
    return artifact_dir / STEP_14_BLANK_PAGES / "blank_detection_empty_exam.txt"


# ---------------------------------------------------------------------------
# Step 15 — Detect exam layout
# ---------------------------------------------------------------------------

def artifact_exam_layout_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_15_LAYOUT / "exam_layout.json"


def artifact_exam_layout_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_15_LAYOUT / "exam_layout.md"


def artifact_exam_layout_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_15_LAYOUT / "exam_layout.xml"


def artifact_exam_layout_raw_path(artifact_dir: Path, fmt: str = "json") -> Path:
    """Step 15: raw AI response before parsing (layout detection)."""
    return artifact_dir / STEP_15_LAYOUT / f"exam_layout_raw.{fmt}"


# ---------------------------------------------------------------------------
# Step 16 — Cut exam PDF (split multi-up layout into single logical pages)
# ---------------------------------------------------------------------------

def artifact_split_exam_pdf_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_16_CUT / "split_exam.pdf"


# ---------------------------------------------------------------------------
# Step 17 — Parse exam PDF
# ---------------------------------------------------------------------------

def artifact_exam_input_pdf_path(artifact_dir: Path) -> Path:
    """Copy of the original exam PDF uploaded to Gemini (1×1 mode)."""
    return artifact_dir / STEP_17_PARSE_EXAM / "exam_input.pdf"


def artifact_exam_questions_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_17_PARSE_EXAM / "exam_questions.json"


def artifact_exam_questions_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_17_PARSE_EXAM / "exam_questions.md"


def artifact_exam_questions_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_17_PARSE_EXAM / "exam_questions.xml"


def artifact_exam_questions_raw_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_17_PARSE_EXAM / "exam_questions_raw.xml"


def artifact_exam_questions_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / STEP_17_PARSE_EXAM / f"exam_questions.{fmt}"


def artifact_exam_questions_raw_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / STEP_17_PARSE_EXAM / f"exam_questions_raw.{fmt}"


# ---------------------------------------------------------------------------
# Step 18 — Detect mark scheme graphics (per-page splits + graphics detection)
# ---------------------------------------------------------------------------

def artifact_mark_scheme_pages_dir(artifact_dir: Path) -> Path:
    """Per-page PDFs (one per mark scheme page) — produced by step 18, consumed by step 19."""
    return artifact_dir / STEP_18_GRAPHICS / "pages"


def artifact_mark_scheme_graphics_dir(artifact_dir: Path) -> Path:
    """Directory of images extracted from the mark scheme."""
    return artifact_dir / STEP_18_GRAPHICS / "graphics"


def artifact_mark_scheme_graphics_json_path(artifact_dir: Path) -> Path:
    """Step 18: detected graphics positions per question."""
    return artifact_dir / STEP_18_GRAPHICS / "mark_scheme_graphics.json"


# ---------------------------------------------------------------------------
# Step 19 — Parse mark scheme
# ---------------------------------------------------------------------------

def artifact_mark_scheme_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_19_PARSE_SCHEME / "mark_scheme.json"


def artifact_mark_scheme_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_19_PARSE_SCHEME / "mark_scheme.md"


def artifact_mark_scheme_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_19_PARSE_SCHEME / "mark_scheme.xml"


def artifact_mark_scheme_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / STEP_19_PARSE_SCHEME / f"mark_scheme.{fmt}"


# ---------------------------------------------------------------------------
# Step 20 — Create report / scaffold cache
# ---------------------------------------------------------------------------

def artifact_scaffold_xml_path(artifact_dir: Path) -> Path:
    """Merged exam + mark scheme XML scaffold cache."""
    return artifact_dir / STEP_20_CREATE_REPORT / "report.xml"


def artifact_scaffold_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_20_CREATE_REPORT / "report.json"


def artifact_scaffold_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_20_CREATE_REPORT / "report.md"


def artifact_short_scaffold_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_20_CREATE_REPORT / "short_report.json"


def artifact_short_scaffold_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_20_CREATE_REPORT / "short_report.md"


# ---------------------------------------------------------------------------
# Step 19 — AI marking blueprints
# ---------------------------------------------------------------------------

def artifact_blueprint_path(artifact_dir: Path, page: int, fmt: str = "yaml") -> Path:
    return artifact_dir / STEP_21_BLUEPRINTS / f"blueprint_page_{page}.{fmt}"


def artifact_blueprint_xml_path(artifact_dir: Path, page: int) -> Path:
    return artifact_dir / STEP_21_BLUEPRINTS / f"blueprint_page_{page}.xml"


def artifact_blueprint_json_path(artifact_dir: Path, page: int) -> Path:
    return artifact_dir / STEP_21_BLUEPRINTS / f"blueprint_page_{page}.json"


def artifact_blueprint_md_path(artifact_dir: Path, page: int) -> Path:
    return artifact_dir / STEP_21_BLUEPRINTS / f"blueprint_page_{page}.md"


# ---------------------------------------------------------------------------
# Step 20 — AI marking
# ---------------------------------------------------------------------------

def artifact_marking_students_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student marking files."""
    return artifact_dir / STEP_22_AI_MARKING / "students"


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
# Step 23 — Per-student reports (XML + MD)
# ---------------------------------------------------------------------------

def artifact_student_reports_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student XML + Markdown reports (step 23)."""
    return artifact_dir / STEP_23_STUDENT_REPORTS / "students"


def artifact_student_report_xml_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_reports_dir(artifact_dir) / f"{safe_student_name(student)}.xml"


def artifact_student_report_md_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_reports_dir(artifact_dir) / f"{safe_student_name(student)}.md"


# Backward-compat alias for callers that haven't migrated to the new name.
artifact_reports_students_dir = artifact_student_reports_dir


# ---------------------------------------------------------------------------
# Step 24 — Class statistics + grade curve
# ---------------------------------------------------------------------------

def artifact_class_stats_json_path(artifact_dir: Path) -> Path:
    """Step 24: class average + curve offset, written before per-student PDFs."""
    return artifact_dir / STEP_24_CLASS_STATS / "class_stats.json"


# ---------------------------------------------------------------------------
# Step 25 — Per-student PDFs (TeX + xelatex output)
# ---------------------------------------------------------------------------

def artifact_student_pdfs_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student .tex + .pdf files (step 25)."""
    return artifact_dir / STEP_25_STUDENT_PDFS / "students"


def artifact_student_report_tex_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdfs_dir(artifact_dir) / f"{safe_student_name(student)}.tex"


def artifact_student_report_pdf_path(artifact_dir: Path, student: str) -> Path:
    return artifact_student_pdfs_dir(artifact_dir) / f"{safe_student_name(student)}.pdf"


# ---------------------------------------------------------------------------
# Step 26 — Class report (XML/MD/TeX/PDF + combined PDF)
# ---------------------------------------------------------------------------

def artifact_class_report_dir(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_26_CLASS_REPORT


def artifact_class_report_xml_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.xml"


def artifact_class_report_md_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.md"


def artifact_class_report_tex_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.tex"


def artifact_class_report_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report.pdf"


def artifact_class_report_combined_pdf_path(artifact_dir: Path) -> Path:
    return artifact_class_report_dir(artifact_dir) / "class_report_combined.pdf"


# ---------------------------------------------------------------------------
# Step 27 — Review queue (side-channel artifact for human spot-check)
# ---------------------------------------------------------------------------

def artifact_review_queue_json_path(artifact_dir: Path) -> Path:
    """Side-channel review queue (medium / low confidence marks).

    Pure side artifact — never loaded by any pipeline step; intended for manual
    spot-checking by the human marker.
    """
    return artifact_dir / STEP_27_REVIEW_QUEUE / "review.json"


def artifact_review_queue_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_27_REVIEW_QUEUE / "review.md"


# ---------------------------------------------------------------------------
# Step 28 — Timing summary (timing only, no accuracy/cost)
# ---------------------------------------------------------------------------

def artifact_timing_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_28_TIMING / "timing.json"


def artifact_timing_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_28_TIMING / "timing.md"


# ---------------------------------------------------------------------------
# Step 29 — Accuracy evaluation (only when ground truth present)
# ---------------------------------------------------------------------------

def artifact_accuracy_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_29_ACCURACY / "accuracy.json"


# ---------------------------------------------------------------------------
# Step 30 — AI costs
# ---------------------------------------------------------------------------

def artifact_cost_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_30_AI_COSTS / "cost.json"


def artifact_cost_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_30_AI_COSTS / "cost.md"


# ---------------------------------------------------------------------------
# Scaffold prompt path (for ai_scaffold.py and scaffold_gemini.py)
# ---------------------------------------------------------------------------

def artifact_scaffold_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Prompt file for scaffold AI calls (layout, exam questions, mark scheme, graphics).

    Routes by content of *name* to the appropriate step folder.
    Use content-only names (no step-number prefix), e.g. ``"exam_questions"``,
    ``"mark_scheme_p1"``, ``"detect_layout"``, ``"mark_scheme_graphics_detect_p1"``.
    """
    # Order matters: check most-specific first (graphics+mark_scheme before plain mark_scheme).
    if "mark_scheme" in name and "graphics" in name:
        return artifact_dir / STEP_18_GRAPHICS / f"{name}_prompt.md"
    if "mark_scheme" in name:
        return artifact_dir / STEP_19_PARSE_SCHEME / f"{name}_prompt.md"
    if "detect" in name or "layout" in name:
        return artifact_dir / STEP_15_LAYOUT / f"{name}_prompt.md"
    return artifact_dir / STEP_17_PARSE_EXAM / f"{name}_prompt.md"


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
    - ``20_*``        → step-20 marking students dir
    - everything else → ``artifact_scaffold_prompt_path``
    """
    if name.startswith("10_cover_p"):
        return artifact_cover_scan_prompt_path(artifact_dir, name)
    if name.startswith("11_name"):
        return artifact_names_prompt_path(artifact_dir, name)
    if name.startswith("13_"):
        return artifact_dir / STEP_13_PAGE_ORDER / f"{name}_prompt.md"
    if name.startswith("14_"):
        return artifact_dir / STEP_14_BLANK_PAGES / f"{name}_prompt.md"
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
            artifact_scaffold_xml_path(ad),                   # 20_create_report/report.xml
            artifact_scaffold_json_path(ad),                  # 20_create_report/report.json
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
