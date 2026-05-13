"""Path builders for student-keyed artifacts.

Covers: the student roster (students.json), per-student answer extraction,
per-student YAML/MD reports, per-student PDF rendering across the six layout
variants and their ``_attempted`` siblings, and the marking-pipeline's
per-student subdirectory for raw marks.
"""

from __future__ import annotations

from pathlib import Path

from xscore.shared.paths._helpers import safe_student_name
from xscore.shared.step_folders import (
    AI_MARKING_DIR,
    EXTRACT_ANSWERS_DIR,
    STUDENT_LIST_DIR,
    STUDENT_PDFS_DIR,
    STUDENT_REPORTS_DIR,
)


# ---------------------------------------------------------------------------
# Student roster (input list of names)
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
# AI marking — per-student raw outputs
# ---------------------------------------------------------------------------

def artifact_marking_students_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student marking subfolders."""
    return artifact_dir / AI_MARKING_DIR / "students"


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
# Per-student reports (YAML + MD)
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
