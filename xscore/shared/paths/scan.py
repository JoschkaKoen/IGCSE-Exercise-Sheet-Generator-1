"""Path builders for scan-side preprocessing artifacts.

Covers: scan cover-page detection, scan geometry (pages per student), subject
detection, per-scan-page student-name detection, page-order check, and the
handwriting-presence vision check.
"""

from __future__ import annotations

from pathlib import Path

from xscore.shared.paths._helpers import safe_student_name
from xscore.shared.step_folders import (
    COVER_SCAN_DIR,
    DETECT_SUBJECT_DIR,
    GEOMETRY_DIR,
    HANDWRITING_DIR,
    PAGE_ORDER_DIR,
    STUDENT_NAMES_DIR,
)


# ---------------------------------------------------------------------------
# Scan cover page (first page only)
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
# Per-page student-name detection
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
# Page-order check
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
# Student handwriting check (per scan page)
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
