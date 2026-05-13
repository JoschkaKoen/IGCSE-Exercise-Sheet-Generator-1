"""Path builders for the marking pipeline.

Covers: parse_instructions (the natural-language prompt parser), the marking
page register (v1 + v2 cross-page-context), blueprints, MCQ corrections,
review queue.
"""

from __future__ import annotations

from pathlib import Path

from xscore.shared.step_folders import (
    AI_MARKING_DIR,
    BLUEPRINTS_DIR,
    BUILD_REGISTER_DIR,
    CROSS_PAGE_CONTEXT_DIR,
    PARSE_INSTRUCTIONS_DIR,
    REVIEW_QUEUE_DIR,
)


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
# Build marking page register (v1)
# ---------------------------------------------------------------------------

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
# AI marking — MCQ corrections audit log
# ---------------------------------------------------------------------------

def artifact_mcq_corrections_path(artifact_dir: Path) -> Path:
    """29_ai_marking/mcq_corrections.yaml — audit log of MCQ outcomes that need
    human attention. Three parallel lists (each with its own ``total_*`` count):
    ``corrections`` (AI overrode the extracted answer), ``not_clear`` (final
    student_answer == "not clear", scored 0), and ``no_answer`` (final
    student_answer == "no answer", scored 0). Regenerated each step-29 run;
    empty lists when nothing qualifies."""
    return artifact_dir / AI_MARKING_DIR / "mcq_corrections.yaml"


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
