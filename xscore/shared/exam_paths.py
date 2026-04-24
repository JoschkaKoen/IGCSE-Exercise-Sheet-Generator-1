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

# Fixed-step subdirectories (not affected by split-mode offset)
STEP_01 = "01_parse_grading_instructions"
STEP_03 = "03_read_student_list"
STEP_05 = "05_detect_blank_pages"
STEP_06 = "06_autorotate"
STEP_07 = "07_deskew"
STEP_08 = "08_exam_geometry"
STEP_09_COVER   = "09_cover_page"
STEP_10_NAMES   = "10_student_names"
STEP_12_PAGE_ORDER  = "12_page_order"
STEP_13_BLANK_PAGES = "13_blank_pages"

# Layout-detection step — split mode only, always step 14 when present
STEP_14_LAYOUT = "14_detect_exam_layout"

# Path of cleaned scan relative to artifact_dir (updated from "7_cleaned_scan.pdf")
CLEANED_SCAN_PDF = STEP_07 + "/cleaned_scan.pdf"

# Backwards-compatible aliases kept for callers not yet migrated to per-step paths
SUBDIR_STUDENTS = "students"
SUBDIR_NAMES    = STEP_10_NAMES + "/names"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def safe_student_name(name: str) -> str:
    """Replace every non-word character in *name* with an underscore."""
    return re.sub(r"[^\w]", "_", name)


def _step_dir(base_step: int, step_offset: int, name: str) -> str:
    """Folder name for a pipeline step that shifts by 1 in split mode.

    *base_step* is the step number in non-split mode.  *step_offset* is 1
    when the multi-up layout-detection step ran, 0 otherwise.
    """
    return f"{base_step + step_offset:02d}_{name}"


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
# Step 10 — Student names
# ---------------------------------------------------------------------------

def artifact_exam_student_list_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_10_NAMES / "exam_student_list.json"


def artifact_exam_student_list_md_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_10_NAMES / "exam_student_list.md"


def artifact_names_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 10: prompt file for name-detection AI calls (one per scan page)."""
    return artifact_dir / STEP_10_NAMES / "names" / f"{name}_prompt.md"


def artifact_cover_scan_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 10: prompt file for cover-page scan AI calls."""
    return artifact_dir / STEP_10_NAMES / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Step 12 — Page order
# ---------------------------------------------------------------------------

def artifact_page_order_txt_path(artifact_dir: Path, student: str) -> Path:
    """Step 12: per-student page-order detection text file."""
    return artifact_dir / STEP_12_PAGE_ORDER / f"page_order_{safe_student_name(student)}.txt"


def artifact_page_order_prompt_path(artifact_dir: Path) -> Path:
    """Step 12: prompt file for page-order AI call."""
    return artifact_dir / STEP_12_PAGE_ORDER / "page_order_prompt.md"


def artifact_page_order_empty_exam_txt_path(artifact_dir: Path) -> Path:
    """Step 12: empty-exam page-order detection text file."""
    return artifact_dir / STEP_12_PAGE_ORDER / "page_order_empty_exam.txt"


# ---------------------------------------------------------------------------
# Step 13 — Blank pages
# ---------------------------------------------------------------------------

def artifact_blank_pages_json_path(artifact_dir: Path) -> Path:
    """Step 13: blank/handwriting detection results JSON."""
    return artifact_dir / STEP_13_BLANK_PAGES / "blank_pages.json"


def artifact_blank_pages_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Step 13: prompt file for blank-page/handwriting AI calls."""
    return artifact_dir / STEP_13_BLANK_PAGES / f"{name}_prompt.md"


def artifact_blank_pages_dir(artifact_dir: Path) -> Path:
    """Step 13: directory of JPEG images from blank-page detection."""
    return artifact_dir / STEP_13_BLANK_PAGES / "blank_pages"


def artifact_blank_detection_txt_path(artifact_dir: Path) -> Path:
    """Step 13: empty-exam blank-detection output text file."""
    return artifact_dir / STEP_13_BLANK_PAGES / "blank_detection_empty_exam.txt"


# ---------------------------------------------------------------------------
# Step 14 — Detect exam layout (split mode only, always step 14 when present)
# ---------------------------------------------------------------------------

def artifact_exam_layout_json_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_14_LAYOUT / "exam_layout.json"


def artifact_exam_layout_markdown_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_14_LAYOUT / "exam_layout.md"


def artifact_exam_layout_xml_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_14_LAYOUT / "exam_layout.xml"


def artifact_exam_layout_raw_path(artifact_dir: Path, fmt: str = "json") -> Path:
    """Step 14: raw AI response before parsing (layout detection)."""
    return artifact_dir / STEP_14_LAYOUT / f"exam_layout_raw.{fmt}"


def artifact_split_exam_pdf_path(artifact_dir: Path) -> Path:
    return artifact_dir / STEP_14_LAYOUT / "split_exam.pdf"


# ---------------------------------------------------------------------------
# Steps 14/15 — Parse exam PDF  (step 14 non-split, step 15 split)
# ---------------------------------------------------------------------------

def artifact_exam_input_pdf_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    """Copy of the original exam PDF uploaded to Gemini (non-split 1×1 mode)."""
    return artifact_dir / _step_dir(14, step_offset, "parse_exam_pdf") / "exam_input.pdf"


def artifact_exam_questions_json_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(14, step_offset, "parse_exam_pdf") / "exam_questions.json"


def artifact_exam_questions_markdown_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(14, step_offset, "parse_exam_pdf") / "exam_questions.md"


def artifact_exam_questions_xml_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(14, step_offset, "parse_exam_pdf") / "exam_questions.xml"


def artifact_exam_questions_raw_xml_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(14, step_offset, "parse_exam_pdf") / "exam_questions_raw.xml"


def artifact_exam_questions_path(artifact_dir: Path, fmt: str = "yaml", step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(14, step_offset, "parse_exam_pdf") / f"exam_questions.{fmt}"


def artifact_exam_questions_raw_path(artifact_dir: Path, fmt: str = "yaml", step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(14, step_offset, "parse_exam_pdf") / f"exam_questions_raw.{fmt}"


# ---------------------------------------------------------------------------
# Steps 15/16 — Parse mark scheme  (step 15 non-split, step 16 split)
# ---------------------------------------------------------------------------

def artifact_mark_scheme_json_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(15, step_offset, "parse_mark_scheme") / "mark_scheme.json"


def artifact_mark_scheme_markdown_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(15, step_offset, "parse_mark_scheme") / "mark_scheme.md"


def artifact_mark_scheme_xml_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(15, step_offset, "parse_mark_scheme") / "mark_scheme.xml"


def artifact_mark_scheme_path(artifact_dir: Path, fmt: str = "yaml", step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(15, step_offset, "parse_mark_scheme") / f"mark_scheme.{fmt}"


def artifact_mark_scheme_graphics_dir(artifact_dir: Path, step_offset: int = 0) -> Path:
    """Directory of images extracted from the mark scheme."""
    return artifact_dir / _step_dir(15, step_offset, "parse_mark_scheme") / "mark_scheme_graphics"


def artifact_mark_scheme_pages_dir(artifact_dir: Path, step_offset: int = 0) -> Path:
    """Temporary directory used during mark-scheme page rendering (cleaned up afterwards)."""
    return artifact_dir / _step_dir(15, step_offset, "parse_mark_scheme") / "mark_scheme_pages"


# ---------------------------------------------------------------------------
# Steps 16/17 — Create report / scaffold cache  (step 16 non-split, step 17 split)
# ---------------------------------------------------------------------------

def artifact_scaffold_xml_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    """Merged exam + mark scheme XML scaffold cache."""
    return artifact_dir / _step_dir(16, step_offset, "create_report") / "report.xml"


def artifact_scaffold_json_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(16, step_offset, "create_report") / "report.json"


def artifact_scaffold_markdown_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(16, step_offset, "create_report") / "report.md"


def artifact_short_scaffold_json_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(16, step_offset, "create_report") / "short_report.json"


def artifact_short_scaffold_markdown_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(16, step_offset, "create_report") / "short_report.md"


# ---------------------------------------------------------------------------
# Steps 17/18 — AI marking blueprints  (step 17 non-split, step 18 split)
# ---------------------------------------------------------------------------

def artifact_blueprint_path(artifact_dir: Path, page: int, fmt: str = "yaml", step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(17, step_offset, "ai_marking_blueprints") / f"blueprint_page_{page}.{fmt}"


def artifact_blueprint_xml_path(artifact_dir: Path, page: int, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(17, step_offset, "ai_marking_blueprints") / f"blueprint_page_{page}.xml"


def artifact_blueprint_json_path(artifact_dir: Path, page: int, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(17, step_offset, "ai_marking_blueprints") / f"blueprint_page_{page}.json"


def artifact_blueprint_md_path(artifact_dir: Path, page: int, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(17, step_offset, "ai_marking_blueprints") / f"blueprint_page_{page}.md"


# ---------------------------------------------------------------------------
# Steps 18/19 — AI marking  (step 18 non-split, step 19 split)
# ---------------------------------------------------------------------------

def artifact_marking_students_dir(artifact_dir: Path, step_offset: int = 0) -> Path:
    """Directory containing per-student marking files."""
    return artifact_dir / _step_dir(18, step_offset, "ai_marking") / "students"


def artifact_marked_path(artifact_dir: Path, student: str, page: int, fmt: str = "yaml", step_offset: int = 0) -> Path:
    return artifact_marking_students_dir(artifact_dir, step_offset) / f"{safe_student_name(student)}_page_{page}.{fmt}"


def artifact_marked_xml_path(artifact_dir: Path, student: str, page: int, step_offset: int = 0) -> Path:
    return artifact_marking_students_dir(artifact_dir, step_offset) / f"{safe_student_name(student)}_page_{page}.xml"


def artifact_marked_md_path(artifact_dir: Path, student: str, page: int, step_offset: int = 0) -> Path:
    return artifact_marking_students_dir(artifact_dir, step_offset) / f"{safe_student_name(student)}_page_{page}.md"


def artifact_marked_failed_path(artifact_dir: Path, student: str, page: int, step_offset: int = 0) -> Path:
    """Failure record when all marking attempts are exhausted for a page."""
    return artifact_marking_students_dir(artifact_dir, step_offset) / f"failed_{safe_student_name(student)}_page_{page}.json"


def artifact_marking_prompt_path(artifact_dir: Path, student: str, page: int, step_offset: int = 0) -> Path:
    """Prompt file saved alongside the marking result for one student page."""
    return artifact_marking_students_dir(artifact_dir, step_offset) / f"{safe_student_name(student)}_page_{page}_prompt.md"


# ---------------------------------------------------------------------------
# Steps 19/20 — Compile reports  (step 19 non-split, step 20 split)
# ---------------------------------------------------------------------------

def artifact_reports_dir(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_dir / _step_dir(19, step_offset, "compile_reports")


def artifact_reports_students_dir(artifact_dir: Path, step_offset: int = 0) -> Path:
    """Directory containing per-student report files."""
    return artifact_reports_dir(artifact_dir, step_offset) / "students"


def artifact_class_report_xml_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_reports_dir(artifact_dir, step_offset) / "class_report.xml"


def artifact_class_report_md_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_reports_dir(artifact_dir, step_offset) / "class_report.md"


def artifact_class_report_tex_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_reports_dir(artifact_dir, step_offset) / "class_report.tex"


def artifact_class_report_pdf_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_reports_dir(artifact_dir, step_offset) / "class_report.pdf"


def artifact_class_report_combined_pdf_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return artifact_reports_dir(artifact_dir, step_offset) / "class_report_combined.pdf"


def artifact_student_report_xml_path(artifact_dir: Path, student: str, step_offset: int = 0) -> Path:
    return artifact_reports_students_dir(artifact_dir, step_offset) / f"{safe_student_name(student)}.xml"


def artifact_student_report_md_path(artifact_dir: Path, student: str, step_offset: int = 0) -> Path:
    return artifact_reports_students_dir(artifact_dir, step_offset) / f"{safe_student_name(student)}.md"


def artifact_student_report_tex_path(artifact_dir: Path, student: str, step_offset: int = 0) -> Path:
    return artifact_reports_students_dir(artifact_dir, step_offset) / f"{safe_student_name(student)}.tex"


def artifact_student_report_pdf_path(artifact_dir: Path, student: str, step_offset: int = 0) -> Path:
    return artifact_reports_students_dir(artifact_dir, step_offset) / f"{safe_student_name(student)}.pdf"


# ---------------------------------------------------------------------------
# Steps 20/21 — Timing summary  (step 20 non-split, step 21 split)
# ---------------------------------------------------------------------------

def _timing_dir(artifact_dir: Path, step_offset: int) -> Path:
    return artifact_dir / _step_dir(20, step_offset, "timing_summary")


def artifact_timing_json_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return _timing_dir(artifact_dir, step_offset) / "timing.json"


def artifact_timing_md_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return _timing_dir(artifact_dir, step_offset) / "timing.md"


def artifact_accuracy_json_path(artifact_dir: Path, step_offset: int = 0) -> Path:
    return _timing_dir(artifact_dir, step_offset) / "accuracy.json"


# ---------------------------------------------------------------------------
# Scaffold prompt path (for ai_scaffold.py and scaffold_gemini.py)
# ---------------------------------------------------------------------------

def artifact_scaffold_prompt_path(artifact_dir: Path, name: str, step_offset: int = 0) -> Path:
    """Prompt file for scaffold AI calls (layout, exam questions, mark scheme).

    Routes by content of *name* to the appropriate step folder.
    Use content-only names (no step-number prefix), e.g. ``"exam_questions"``,
    ``"mark_scheme_p1"``, ``"detect_layout"``.
    """
    # Layout detection — always step 14, no offset
    if "detect" in name or "layout" in name:
        return artifact_dir / STEP_14_LAYOUT / f"{name}_prompt.md"
    # Mark scheme — step 15 non-split, 16 split
    if "mark_scheme" in name:
        return artifact_dir / _step_dir(15, step_offset, "parse_mark_scheme") / f"{name}_prompt.md"
    # Fallback: parse-exam-pdf folder (step 14 non-split, 15 split)
    return artifact_dir / _step_dir(14, step_offset, "parse_exam_pdf") / f"{name}_prompt.md"


# ---------------------------------------------------------------------------
# Deprecated generic prompt router (kept for callers not yet migrated)
# ---------------------------------------------------------------------------

def artifact_prompt_path(artifact_dir: Path, name: str) -> Path:
    """DEPRECATED — use the specific prompt path functions instead.

    Routing:
    - ``10_name*``    → step-10 names subdir
    - ``10_cover_p*`` → step-10 root
    - ``12_*``        → step-12 page-order root
    - ``13_*``        → step-13 blank-pages root
    - ``8_*``         → step-8 geometry root
    - ``3_*``         → step-3 root
    - ``19_*``        → step-18 marking students dir (step_offset=0 assumed)
    - everything else → ``artifact_scaffold_prompt_path`` (step_offset=0)
    """
    if name.startswith("10_name"):
        return artifact_names_prompt_path(artifact_dir, name)
    if name.startswith("10_cover_p"):
        return artifact_cover_scan_prompt_path(artifact_dir, name)
    if name.startswith("12_"):
        return artifact_dir / STEP_12_PAGE_ORDER / f"{name}_prompt.md"
    if name.startswith("13_"):
        return artifact_dir / STEP_13_BLANK_PAGES / f"{name}_prompt.md"
    if name.startswith("8_"):
        return artifact_geometry_prompt_path(artifact_dir, name)
    if name.startswith("3_"):
        return artifact_dir / STEP_03 / f"{name}_prompt.md"
    if name.startswith("19_"):
        return artifact_marking_students_dir(artifact_dir, 0) / f"{name}_prompt.md"
    return artifact_scaffold_prompt_path(artifact_dir, name, step_offset=0)


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
            artifact_scaffold_xml_path(ad, step_offset=0),    # new: 16_create_report/report.xml
            artifact_scaffold_xml_path(ad, step_offset=1),    # new: 17_create_report/report.xml
            artifact_scaffold_json_path(ad, step_offset=0),   # new: 16_create_report/report.json
            artifact_scaffold_json_path(ad, step_offset=1),   # new: 17_create_report/report.json
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
