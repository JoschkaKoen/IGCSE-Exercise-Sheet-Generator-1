"""Pydantic schemas, JSON schema constants, model-config functions, and prompt strings
for the AI scaffold extraction pipeline.

Prompt bodies live in ``xscore/prompts/*.md`` (loaded via :mod:`xscore.prompts.loader`)
so they can be versioned and edited without a Python reload. The
``_build_user_exam_prompt`` helper stays in this module because its body is
assembled from the detected layout at runtime.
"""

from __future__ import annotations

from pydantic import BaseModel

from eXercise.ai_client import parse_model_spec
from xscore.config import (
    DETECT_LAYOUT_MODEL,
    DETECT_SCHEME_GRAPHICS_MODEL,
    READ_EXAM_PDF_MODEL,
    READ_MARK_SCHEME_MODEL,
)
from xscore.prompts.loader import load_prompt as _load_prompt


# ---------------------------------------------------------------------------
# Pydantic schemas (used for Gemini JSON-mode responses)
# ---------------------------------------------------------------------------

class _LayoutDetectSchema(BaseModel):
    rows: int = 1
    cols: int = 1
    reading_order: list[list[int]] = []
    # Each entry is [row, col] (1-based). Order = left-to-right reading sequence.
    # 4-up reading order: [[1,1],[1,2],[2,1],[2,2]]
    # 2-up landscape: [[1,1],[1,2]]
    # Empty list = fallback to row-major (left→right, top→bottom)


_LAYOUT_DETECT_JSON_SCHEMA: dict = _LayoutDetectSchema.model_json_schema()


class _SchemeGraphic(BaseModel):
    question_number: str   # "3(b)(ii)" as printed in the mark scheme
    bbox: list[int]        # [x_min, y_min, x_max, y_max] on 0-1000 scale
    description: str       # "circuit diagram"


class _SchemePageGraphics(BaseModel):
    graphics: list[_SchemeGraphic]


_SCHEME_GRAPHICS_JSON_SCHEMA: dict = _SchemePageGraphics.model_json_schema()


# ---------------------------------------------------------------------------
# Model config — read from xscore/config.py constants (defaults match default.env)
# ---------------------------------------------------------------------------

def _exam_pdf_model_config() -> tuple[str, int | None, int | None]:
    return parse_model_spec(READ_EXAM_PDF_MODEL)


def _mark_scheme_model_config() -> tuple[str, int | None, int | None]:
    return parse_model_spec(READ_MARK_SCHEME_MODEL)


def _layout_detect_model_config() -> tuple[str, int | None, int | None]:
    return parse_model_spec(DETECT_LAYOUT_MODEL)


def _detect_scheme_graphics_model_config() -> tuple[str, int | None, int | None]:
    return parse_model_spec(DETECT_SCHEME_GRAPHICS_MODEL)


# ---------------------------------------------------------------------------
# Prompts — bodies live in xscore/prompts/*.md (see xscore/prompts/loader.py).
# `prompt_metadata(name)["version"]` is the version pin for run manifests.
# ---------------------------------------------------------------------------

# Static bodies are loaded once at import time so callers see plain strings,
# preserving the pre-refactor module-level API. Combined .md files use
# section= to extract the role-specific portion.
_USER_EXAM = _load_prompt("parse_exam_pdf_xml", section="user")[1]
_USER_GRAPHICS = _load_prompt("detect_mark_scheme_graphics", section="user")[1]
_SYSTEM_LAYOUT = _load_prompt("detect_exam_layout", section="system")[1]
_USER_LAYOUT = _load_prompt("detect_exam_layout", section="user")[1]


def make_system_scheme_prompt(prompt_name: str, *, is_cs: bool = False) -> str:
    """Load the SYSTEM section of a scheme-parse prompt; if ``is_cs`` also append
    the CODE_FORMATTING section so the AI emits ``\\texttt`` / ``\\begin{alltt}``
    for code in criterion text.

    *prompt_name* is one of ``"parse_mark_scheme_xml"`` / ``"_yaml"`` / ``"_json"``.
    """
    base = _load_prompt(prompt_name, section="system")[1]
    if is_cs:
        code = _load_prompt(prompt_name, section="code_formatting")[1]
        return base + "\n\n" + code.rstrip("\n")
    return base


def make_system_exam_prompt(prompt_name: str, *, is_cs: bool = False) -> str:
    """Load the SYSTEM section of an exam-paper-parse prompt; if ``is_cs`` also
    append the CODE_FORMATTING section so the AI emits ``\\texttt`` /
    ``\\begin{alltt}`` for code in question text and MCQ options.

    *prompt_name* is one of ``"parse_exam_pdf_xml"`` / ``"_yaml"`` / ``"_json"``.
    """
    base = _load_prompt(prompt_name, section="system")[1]
    if is_cs:
        code = _load_prompt(prompt_name, section="code_formatting")[1]
        return base + "\n\n" + code.rstrip("\n")
    return base

# Reading-order labels for 4-up subpages, indexed by (row, col).
_QUAD = {
    (1, 1): "top-left", (1, 2): "top-right",
    (2, 1): "bottom-left", (2, 2): "bottom-right",
}


def _build_user_exam_prompt(
    layout_result: "_LayoutDetectSchema | None",
    is_split: bool,
    n_split_pages: int,
) -> str:
    """Build the step-9 user prompt, injecting the layout known from step 8.

    Falls back to _USER_EXAM (which asks the AI to detect the layout) when
    layout_result is None, i.e. when split_subpages=False and step 8 was skipped.
    """
    if layout_result is None:
        return _USER_EXAM

    if is_split:
        rows, cols = layout_result.rows, layout_result.cols
        order = layout_result.reading_order or [
            [r + 1, c + 1] for r in range(rows) for c in range(cols)
        ]
        cells = len(order)
        order_labels = [_QUAD.get((rc[0], rc[1]), f"r{rc[0]}c{rc[1]}") for rc in order]
        reading_order_str = " → ".join(order_labels)

        mapping_lines = []
        for split_p in range(1, n_split_pages + 1):
            phys = (split_p - 1) // cells + 1
            rc = order[(split_p - 1) % cells]
            label = _QUAD.get((rc[0], rc[1]), f"row {rc[0]} col {rc[1]}")
            mapping_lines.append(
                f"  PDF page {split_p} → "
                f"page=\"{phys}\" subpage_row=\"{rc[0]}\" subpage_col=\"{rc[1]}\" ({label})"
            )
        mapping = "\n".join(mapping_lines)

        header = (
            f"The layout of this exam has already been detected: "
            f"{rows}×{cols} grid, reading order: {reading_order_str}.\n"
            f"This PDF has been pre-split into {n_split_pages} individual sub-pages "
            f"(one per PDF page).\n\n"
            "Return ONLY well-formed XML, no markdown fences or other text outside the XML.\n\n"
            f'Set the root element as: <exam rows="{rows}" cols="{cols}">\n\n'
            "Use this mapping to set page, subpage_row, and subpage_col for each question\n"
            "based on which PDF page the question physically appears on:\n"
            f"{mapping}\n\n"
        )
        page_desc      = "exam page from the mapping above"
        subpage_r_desc = "subpage_row from the mapping above"
        subpage_c_desc = "subpage_col from the mapping above"
    else:
        # 1×1 non-split (layout_result.rows == layout_result.cols == 1 always here)
        header = (
            "The layout of this exam has already been detected: 1×1 (one sub-page per page).\n\n"
            "Return ONLY well-formed XML, no markdown fences or other text outside the XML.\n\n"
            'Set the root element as: <exam rows="1" cols="1">\n\n'
            'Set subpage_row="1" and subpage_col="1" for every question.\n\n'
        )
        page_desc      = "1-based page number where this question first appears"
        subpage_r_desc = "always 1"
        subpage_c_desc = "always 1"

    common_tail = (
        "Extract every question and sub-question at every nesting level as <question> elements.\n"
        "Nested sub-questions are child <question> elements inside their parent.\n\n"
        "Each <question> must have these attributes:\n"
        '- number: the label as printed, run-together — "9", then "9a", then "9ai"'
        " (no parentheses or spaces)\n"
        "- type: one of multiple_choice | short_answer | calculation | long_answer\n"
        f"- page: {page_desc}\n"
        f"- subpage_row: {subpage_r_desc}\n"
        f"- subpage_col: {subpage_c_desc}\n"
        "- marks: integer mark allocation from [N] brackets; 0 if not printed\n\n"
        "Each <question> must contain:\n"
        "- <text>: complete question text in markdown; $...$ for inline math, $$...$$ for display math\n"
        '- <option letter="A">text</option>: for multiple_choice only — one per answer option\n'
        "- child <question> elements for any sub-questions\n\n"
        "In XML text content use &lt; for <, &gt; for >, &amp; for &.\n"
    )

    return header + common_tail
