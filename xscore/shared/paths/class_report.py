"""Path builders for class-level report artifacts.

Covers: class statistics, the class report (XML/MD/TeX/PDF, including the
combined-with-students variants and the 2-up reflow), the embedded charts,
the scheme-graphics check PDF, and the class marks XLSX export.
"""

from __future__ import annotations

from pathlib import Path

from xscore.shared.step_folders import CLASS_REPORT_DIR, CLASS_STATS_DIR


# ---------------------------------------------------------------------------
# Class statistics + grade curve
# ---------------------------------------------------------------------------

def artifact_class_stats_json_path(artifact_dir: Path) -> Path:
    """Class average + curve offset, written before per-student PDFs."""
    return artifact_dir / CLASS_STATS_DIR / "class_stats.json"


# ---------------------------------------------------------------------------
# Class report (XML/MD/TeX/PDF + combined PDF + charts)
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
