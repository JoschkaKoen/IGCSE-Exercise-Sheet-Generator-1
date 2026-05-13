"""Path builders for scaffold (exam structure + mark scheme) artifacts.

Covers: empty-exam cover detection, empty-exam page classification, exam
layout detection, the cut-exam PDF, exam-scaffold and exam-questions
extraction, mark-scheme detection / assignment / parsing, scheme-graphic
transcription, the scaffold cache itself, and the scaffold-prompt router.

Also hosts the scaffold-cache lookup helpers (``find_scaffold_cache_file``,
``is_completed_run``).
"""

from __future__ import annotations

from pathlib import Path

from xscore.shared.paths._helpers import exam_artifact_dir
from xscore.shared.step_folders import (
    ASSIGN_QUESTIONS_DIR,
    COVER_EMPTY_DIR,
    CREATE_REPORT_DIR,
    CUT_EXAM_DIR,
    EMPTY_EXAM_CLASSIFY_DIR,
    EXTRACT_QUESTION_NUMBERS_DIR,
    EXTRACT_QUESTIONS_DIR,
    LAYOUT_DIR,
    PARSE_SCHEME_DIR,
    SCHEME_GRAPHICS_DIR,
    TRANSCRIBE_SCHEME_GRAPHICS_DIR,
)


# ---------------------------------------------------------------------------
# Cover page detection (empty exam)
# ---------------------------------------------------------------------------

def artifact_cover_page_dir(artifact_dir: Path) -> Path:
    """Directory for empty-exam cover-page detection artifacts."""
    return artifact_dir / COVER_EMPTY_DIR


# ---------------------------------------------------------------------------
# Empty-exam page classification (vision)
# ---------------------------------------------------------------------------

def artifact_empty_exam_classifications_json_path(artifact_dir: Path) -> Path:
    """Per-empty-exam-page classifications (page_type + page_number) JSON."""
    return artifact_dir / EMPTY_EXAM_CLASSIFY_DIR / "empty_exam_classifications.json"


def artifact_empty_exam_pages_dir(artifact_dir: Path) -> Path:
    """Directory of per-page artifacts for the empty-exam classifier (classify_empty_exam_pages).

    Files are PDFs on the Gemini path (one-page slices) and JPEGs on the
    rasterized fallback. Prompt sidecars land here too.
    """
    return artifact_dir / EMPTY_EXAM_CLASSIFY_DIR / "empty_exam_pages"


# ---------------------------------------------------------------------------
# Exam layout detection
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


def artifact_exam_input_pdf_path(artifact_dir: Path) -> Path:
    """Copy of the original exam PDF (1×1 mode) — ``exam_input.pdf``."""
    return artifact_dir / CUT_EXAM_DIR / "exam_input.pdf"


# ---------------------------------------------------------------------------
# Extract question numbers from empty exam (extract_exam_question_numbers)
# ---------------------------------------------------------------------------

def artifact_exam_scaffold_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    """Intermediate scaffold — number/type/page/subpage/marks, no text."""
    return artifact_dir / EXTRACT_QUESTION_NUMBERS_DIR / f"exam_scaffold.{fmt}"


def artifact_exam_scaffold_raw_path(artifact_dir: Path, fmt: str = "yaml") -> Path:
    return artifact_dir / EXTRACT_QUESTION_NUMBERS_DIR / f"exam_scaffold_raw.{fmt}"


# ---------------------------------------------------------------------------
# Extract questions from empty exam (extract_exam_questions — text + options per question)
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
    """Per-page PDFs from the post-cut exam PDF — produced and consumed by extract_exam_questions (extract_exam_questions)."""
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
    """Per-graphic textual descriptions consumed by ai_marking (ai_marking)."""
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
    # Catch-all: per-page extract-exam-questions prompts (extract_exam_questions).
    return artifact_dir / EXTRACT_QUESTIONS_DIR / f"{name}_prompt.txt"


# ---------------------------------------------------------------------------
# Scaffold cache lookup
# ---------------------------------------------------------------------------

def find_scaffold_cache_file(
    exam_folder: Path, output_base: str | Path = "output/xscore"
) -> Path | None:
    """First existing scaffold cache in the per-exam artifact tree."""
    for base in (output_base, "output"):
        ad = exam_artifact_dir(exam_folder, base)
        for p in (
            artifact_scaffold_yaml_path(ad),
            artifact_scaffold_xml_path(ad),
            artifact_scaffold_json_path(ad),
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
    """True iff *run_dir* contains a finished scaffold report (YAML or legacy XML/JSON sidecar)."""
    return any(
        p.exists()
        for p in (
            artifact_scaffold_yaml_path(run_dir),
            artifact_scaffold_xml_path(run_dir),
            artifact_scaffold_json_path(run_dir),
        )
    )
