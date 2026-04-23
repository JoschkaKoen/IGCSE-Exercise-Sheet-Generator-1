"""Paths for per-exam derived artifacts (under ``output/<stem>/`` by default)."""

from __future__ import annotations

import re
from pathlib import Path

# Re-exported here for backwards compatibility; canonical location is find_exam_folder.py.
from xscore.marking.find_exam_folder import validate_input_files as validate_input_files  # noqa: F401


SUBDIR_STUDENTS = "students"   # per-student files (marking results + reports)
SUBDIR_NAMES    = "8_names"    # name-detection prompts (one per scan page — many files)


def safe_student_name(name: str) -> str:
    """Replace every non-word character in *name* with an underscore for use in filenames."""
    return re.sub(r"[^\w]", "_", name)

def artifact_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Path for a saved AI prompt.

    All prompt files end in ``_prompt.md`` so they are visually distinct from
    response / artifact files in the output directory.

    Routing:
    - ``8_name_*``  → ``8_names/<name>_prompt.md``   (many per exam — own subdir)
    - ``14_*``      → ``students/<name>_prompt.md``   (per-student, avoids collision)
    - everything else → ``<name>_prompt.md``          (flat in root)
    """
    if name.startswith("8_name"):
        return artifact_dir / SUBDIR_NAMES / f"{name}_prompt.md"
    if name.startswith("14_"):
        return artifact_dir / SUBDIR_STUDENTS / f"{name}_prompt.md"
    return artifact_dir / f"{name}_prompt.md"


def safe_path_stem(stem: str) -> str:
    """Stable directory / filename fragment from a PDF stem (no spaces, slashes, or traversal)."""
    # Remove null bytes and replace path-unsafe characters.
    stem = stem.replace("\x00", "").replace(" ", "_").replace("/", "_").replace("\\", "_")
    # Prevent directory traversal by replacing ".." components.
    parts = stem.split("_")
    parts = [p if p != ".." else "__" for p in parts]
    return "_".join(parts) or "_"


def exam_artifact_dir(exam_folder: Path, output_base: str | Path = "output/xscore") -> Path:
    """Directory for all per-exam artifacts.

    *exam_folder* is the exam input directory (raw PDFs, roster). Artifacts live
    under ``output/xscore/<stem>/`` by default, where *stem* is the folder name
    with spaces replaced by underscores.
    """
    stem = exam_folder.name.replace(" ", "_")
    return Path(output_base) / stem


def artifact_scaffold_json_path(artifact_dir: Path) -> Path:
    """Step 12: canonical merged report JSON."""
    return artifact_dir / "12_report.json"


def artifact_scaffold_markdown_path(artifact_dir: Path) -> Path:
    """Step 12: human-readable report beside :func:`artifact_scaffold_json_path`."""
    return artifact_dir / "12_report.md"


def artifact_short_scaffold_json_path(artifact_dir: Path) -> Path:
    """Step 12: short report JSON — same as 12_report.json but without the student list."""
    return artifact_dir / "12_short_report.json"


def artifact_short_scaffold_markdown_path(artifact_dir: Path) -> Path:
    """Step 12: short report markdown — same as 12_report.md but without the student list."""
    return artifact_dir / "12_short_report.md"


def legacy_flat_artifact_scaffold_cache_path(artifact_dir: Path) -> Path:
    """Deprecated: older runs stored the cache as ``scaffold_cache.json`` in the run folder."""
    return artifact_dir / "scaffold_cache.json"


def legacy_artifact_scaffold_cache_path(artifact_dir: Path) -> Path:
    """Older layout: cache lived under ``scaffolds/`` inside *artifact_dir*."""
    return artifact_dir / "scaffolds" / "scaffold_cache.json"


def artifact_students_json_path(artifact_dir: Path) -> Path:
    """Step 3: student roster as a JSON array of name strings."""
    return artifact_dir / "3_students.json"


def artifact_students_markdown_path(artifact_dir: Path) -> Path:
    """Step 3: human-readable numbered student list."""
    return artifact_dir / "3_students.md"


def artifact_exam_questions_json_path(artifact_dir: Path) -> Path:
    """Step 10: raw Gemini exam-parse output (no answers/criteria yet)."""
    return artifact_dir / "10_exam_questions.json"


def artifact_exam_questions_markdown_path(artifact_dir: Path) -> Path:
    """Step 10: human-readable exam questions without mark-scheme annotations."""
    return artifact_dir / "10_exam_questions.md"


def artifact_marked_xml_path(artifact_dir: Path, student: str, page: int) -> Path:
    """Step 14: AI-filled marking blueprint for one student's scan page (XML)."""
    return artifact_dir / SUBDIR_STUDENTS / f"14_marked_{safe_student_name(student)}_{page}.xml"


def artifact_student_report_xml_path(artifact_dir: Path, student: str) -> Path:
    """Step 15: merged student report (XML)."""
    return artifact_dir / SUBDIR_STUDENTS / f"15_student_report_{safe_student_name(student)}.xml"


def artifact_class_report_xml_path(artifact_dir: Path) -> Path:
    """Step 15: class-wide summary (XML)."""
    return artifact_dir / "15_class_report.xml"


def artifact_mark_scheme_json_path(artifact_dir: Path) -> Path:
    """Step 11: raw Gemini mark-scheme output before merge into question tree."""
    return artifact_dir / "11_mark_scheme.json"


def artifact_mark_scheme_markdown_path(artifact_dir: Path) -> Path:
    """Step 11: human-readable mark scheme (per-question sections with criteria)."""
    return artifact_dir / "11_mark_scheme.md"


def artifact_exam_questions_raw_xml_path(artifact_dir: Path) -> Path:
    """Step 10: raw XML string returned by Gemini before parsing."""
    return artifact_dir / "10_exam_questions_raw.xml"


def artifact_mark_scheme_xml_path(artifact_dir: Path) -> Path:
    """Step 11: canonical XML (preprocessed Gemini response)."""
    return artifact_dir / "11_mark_scheme.xml"


def artifact_exam_questions_xml_path(artifact_dir: Path) -> Path:
    """Step 10: canonical XML after page remapping."""
    return artifact_dir / "10_exam_questions.xml"


def artifact_scaffold_xml_path(artifact_dir: Path) -> Path:
    """Step 12: merged exam + mark scheme XML scaffold cache."""
    return artifact_dir / "12_report.xml"


def artifact_blueprint_xml_path(artifact_dir: Path, page: int) -> Path:
    """Step 13: XML marking blueprint for one exam page."""
    return artifact_dir / f"13_ai_marking_blueprint_{page}.xml"


def artifact_exam_layout_xml_path(artifact_dir: Path) -> Path:
    """Step 9: layout detection result as XML."""
    return artifact_dir / "9_exam_layout.xml"


def artifact_exam_layout_json_path(artifact_dir: Path) -> Path:
    """Step 9: layout detection result — rows, cols, reading order."""
    return artifact_dir / "9_exam_layout.json"


def artifact_exam_layout_markdown_path(artifact_dir: Path) -> Path:
    """Step 9: human-readable layout detection summary."""
    return artifact_dir / "9_exam_layout.md"


def artifact_split_exam_pdf_path(artifact_dir: Path) -> Path:
    """Step 9 (multi-up): exam PDF cut into individual sub-pages in reading order."""
    return artifact_dir / "9_split_exam.pdf"


def artifact_exam_input_pdf_path(artifact_dir: Path) -> Path:
    """Step 9 (1×1): copy of the original exam PDF uploaded to Gemini."""
    return artifact_dir / "9_exam_input.pdf"


def extract_answers_output_dir(
    pdf_stem: str, output_base: str | Path = "output"
) -> Path:
    """Directory for one ``extract_answers`` run: ``output/extract_answers/<safe_stem>/``."""
    return Path(output_base) / "extract_answers" / safe_path_stem(pdf_stem)


CLEANED_SCAN_PDF = "7_cleaned_scan.pdf"


# ---------------------------------------------------------------------------
# Steps 8–16: geometry, marking pipeline artifacts
# ---------------------------------------------------------------------------

def artifact_geometry_json_path(artifact_dir: Path) -> Path:
    """Step 8: exam geometry (page counts, student count)."""
    return artifact_dir / "8_exam_geometry.json"


def artifact_geometry_md_path(artifact_dir: Path) -> Path:
    """Step 8: human-readable exam geometry table."""
    return artifact_dir / "8_exam_geometry.md"


def artifact_exam_student_list_json_path(artifact_dir: Path) -> Path:
    """Step 8: scan-detected student list with page assignments."""
    return artifact_dir / "8_exam_student_list.json"


def artifact_exam_student_list_md_path(artifact_dir: Path) -> Path:
    """Step 8: human-readable student-to-page assignment table."""
    return artifact_dir / "8_exam_student_list.md"


def artifact_blueprint_json_path(artifact_dir: Path, page: int) -> Path:
    """Step 13: empty AI marking blueprint for one exam page."""
    return artifact_dir / f"13_ai_marking_blueprint_{page}.json"


def artifact_blueprint_md_path(artifact_dir: Path, page: int) -> Path:
    """Step 13: human-readable blueprint summary for one exam page."""
    return artifact_dir / f"13_ai_marking_blueprint_{page}.md"


def artifact_marked_md_path(artifact_dir: Path, student: str, page: int) -> Path:
    """Step 14: human-readable marking result for one student's scan page."""
    return artifact_dir / SUBDIR_STUDENTS / f"14_marked_{safe_student_name(student)}_{page}.md"


def artifact_marked_failed_path(artifact_dir: Path, student: str, page: int) -> Path:
    """Step 14: failure record when all marking attempts are exhausted for a page.

    Uses a distinct ``14_failed_*`` prefix so it is never matched by the
    ``14_marked_*_*.json`` glob used by merge_reports.py.
    """
    return artifact_dir / SUBDIR_STUDENTS / f"14_failed_{safe_student_name(student)}_{page}.json"


def artifact_marking_students_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student marking JSON files (step 14)."""
    return artifact_dir / SUBDIR_STUDENTS


def artifact_student_report_md_path(artifact_dir: Path, student: str) -> Path:
    """Step 15: human-readable student report."""
    return artifact_dir / SUBDIR_STUDENTS / f"15_student_report_{safe_student_name(student)}.md"


def artifact_student_report_tex_path(artifact_dir: Path, student: str) -> Path:
    """Step 15: LaTeX source for student report PDF."""
    return artifact_dir / SUBDIR_STUDENTS / f"15_student_report_{safe_student_name(student)}.tex"


def artifact_reports_students_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student report files (step 15)."""
    return artifact_dir / SUBDIR_STUDENTS


def artifact_class_report_md_path(artifact_dir: Path) -> Path:
    """Step 15: human-readable class report."""
    return artifact_dir / "15_class_report.md"


def artifact_class_report_tex_path(artifact_dir: Path) -> Path:
    """Step 15: LaTeX source for class report PDF."""
    return artifact_dir / "15_class_report.tex"


def artifact_class_report_pdf_path(artifact_dir: Path) -> Path:
    """Step 15: compiled class report PDF."""
    return artifact_dir / "15_class_report.pdf"


def artifact_timing_json_path(artifact_dir: Path) -> Path:
    """Step 16: marking pipeline timing data."""
    return artifact_dir / "16_timing.json"


def artifact_timing_md_path(artifact_dir: Path) -> Path:
    """Step 16: human-readable timing table."""
    return artifact_dir / "16_timing.md"


def artifact_accuracy_json_path(artifact_dir: Path) -> Path:
    """Step 16: recognition accuracy vs ground truth."""
    return artifact_dir / "16_accuracy.json"


def find_scaffold_cache_file(
    exam_folder: Path, output_base: str | Path = "output/xscore"
) -> Path | None:
    """First existing scaffold cache: artifact dir, then legacy locations under *exam_folder*.

    Checks ``output_base/<stem>/`` first, then the legacy ``output/<stem>/`` tree so
    runs created before the output-folder split are still found.
    """
    for base in (output_base, "output"):   # new location first, then legacy
        ad = exam_artifact_dir(exam_folder, base)
        for p in (
            artifact_scaffold_xml_path(ad),           # 12_report.xml   (current — XML)
            artifact_scaffold_json_path(ad),          # 12_report.json  (current JSON)
            ad / "exam" / "12_report.json",           # legacy: was in exam/ subdir
            ad / "scaffold" / "12_report.json",       # legacy: was in scaffold/ subdir
            ad / "6_report.json",                     # legacy: older name
            ad / "6_scaffold.json",                   # older legacy name
            ad / "5_scaffold.json",                   # older legacy name
            ad / "1_scaffold.json",                   # oldest legacy name
            legacy_flat_artifact_scaffold_cache_path(ad),
            legacy_artifact_scaffold_cache_path(ad),
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
