"""Pydantic schemas, JSON schema constants, model-config functions, and prompt strings
for the AI scaffold extraction pipeline.
"""

from __future__ import annotations

from pydantic import BaseModel

from eXercise.ai_client import parse_model_effort
from xscore.config import (
    DETECT_LAYOUT_MODEL,
    DETECT_SCHEME_GRAPHICS_MODEL,
    READ_EXAM_PDF_MODEL,
    READ_MARK_SCHEME_MODEL,
)


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

def _exam_pdf_model_config() -> tuple[str, str | None]:
    return parse_model_effort(READ_EXAM_PDF_MODEL)


def _mark_scheme_model_config() -> tuple[str, str | None]:
    return parse_model_effort(READ_MARK_SCHEME_MODEL)


def _layout_detect_model_config() -> tuple[str, str | None]:
    return parse_model_effort(DETECT_LAYOUT_MODEL)


def _detect_scheme_graphics_model_config() -> tuple[str, str | None]:
    return parse_model_effort(DETECT_SCHEME_GRAPHICS_MODEL)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_EXAM = (
    "You are an expert at reading Cambridge IGCSE exam papers. "
    "Extract every question and sub-question as structured XML."
)

_USER_EXAM = """\
Return ONLY well-formed XML, no markdown fences or other text outside the XML.

First identify the page layout and set it as attributes on the root element:
  <exam rows="1 or 2" cols="1 or 2">

A standard single-page exam: rows="1" cols="1".
A 4-up exam (2×2 grid): rows="2" cols="2".

Then extract every question and sub-question at every nesting level as <question> elements.
Nested sub-questions are child <question> elements inside their parent.

Each <question> must have these attributes:
- number: the label as printed, run-together — "9", then "9a", then "9ai" (no parentheses or spaces)
- type: one of multiple_choice | short_answer | calculation | long_answer
- page: 1-based page number where this question first appears
- subpage_row: 1-based row of the quadrant (1 for 1x1 layout; 1=top, 2=bottom for 2x2)
- subpage_col: 1-based column of the quadrant (1 for 1x1 layout; 1=left, 2=right for 2x2)
- marks: integer mark allocation from [N] brackets; 0 if not printed

IMPORTANT — subpage assignment: assign based solely on where the question is
physically printed. The same question number can appear more than once in the same
quadrant; assign the quadrant each instance is physically in.

Each <question> must contain:
- <text>: complete question text in markdown; $...$ for inline math, $$...$$ for display math
- <option letter="A">text</option>: for multiple_choice only — one per answer option
- child <question> elements for any sub-questions

In XML text content use &lt; for <, &gt; for >, &amp; for &.
"""


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

    _QUAD = {
        (1, 1): "top-left", (1, 2): "top-right",
        (2, 1): "bottom-left", (2, 2): "bottom-right",
    }

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
            f"{rows}\u00d7{cols} grid, reading order: {reading_order_str}.\n"
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
            "The layout of this exam has already been detected: 1\u00d71 (one sub-page per page).\n\n"
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


_SYSTEM_SCHEME = (
    "You are an expert at reading Cambridge IGCSE mark schemes. "
    "Extract marking criteria as structured XML."
)

_USER_SCHEME = """\
Return ONLY well-formed XML, no markdown fences or other text outside the XML.

Below is a scaffold listing every question from the exam. Fill in the correct_answer \
attribute and add a <criterion> child for each question, based on the mark scheme PDF.

{scaffold}

For each <question>:
- correct_answer attribute: model answer with $...$ for inline math \
(e.g. "$1.5 \\times 10^{{11}}$ m"); for multiple-choice just the letter
- <criterion mark=""> child: extract the COMPLETE marking scheme text for this question \
as a single <criterion mark=""> element containing a LaTeX-formatted block. \
Include ALL content — introductory sentences (e.g. "One mark per each correct character \
in the correct order:"), bullet lists, numbered lists, tables, bold text, and any other \
mark scheme text. Do not skip any text associated with the question's marking criteria.
- LaTeX formatting rules for the block:
    bold text           → \\textbf{{...}}
    unordered lists     → \\begin{{itemize}}\\item first\\item second\\end{{itemize}}
    ordered/numbered lists → \\begin{{enumerate}}\\item first\\item second\\end{{enumerate}}
    tables              → \\begin{{tabular}}{{col-spec}} cell & cell \\\\ next row \\end{{tabular}} \
(infer col-spec as l/c/r per column)
    inline math         → $...$
    output contract     → your text is placed verbatim into LaTeX table cells (p{{}} columns).
                          Escape characters that appear as literal text (not LaTeX syntax):
                          % → \\%,   $ → \\$,   # → \\#,   _ → \\_,
                          {{ → \\{{,   }} → \\}},   backslash → \\textbackslash{{}},
                          literal ampersand → &amp; (standard XML; \\& for LaTeX is added automatically).
                          Use \\newline for explicit line breaks between prose sentences only.
                          NEVER use \\newline immediately after \\begin{{...}} or before \\end{{...}}.
                          List items begin directly with \\item — no \\newline between them.
                          Correct: \\begin{{itemize}}\\item first\\item second\\end{{itemize}}
                          Wrong:   \\begin{{itemize}}\\newline\\item first\\newline\\end{{itemize}}
    CRITICAL — the entire <criterion> text must be a single unbroken line.
               No literal newlines (\\n) anywhere inside the criterion — not between list items,
               not before \\begin{{...}}, not after \\end{{...}}, not anywhere.
               Wrong: "Any two from:\\n\\begin{{itemize}}\\n\\item To save space\\n\\end{{itemize}}"
               Right: "Any two from: \\begin{{itemize}}\\item To save space\\item To transmit faster\\end{{itemize}}"
    plain prose and introductory sentences are written verbatim (no special wrapping)
- For multiple_choice questions: set correct_answer only; no <criterion> children needed
- Keep every <question> element present — even if marks cannot be found for it
- In XML text use &lt; for <, &gt; for >, &amp; for &
"""

_USER_GRAPHICS = (
    "Identify diagrams, figures, and illustrations on this page — things a human would "
    "describe as 'a drawing' or 'a figure'. This includes circuit diagrams, logic gate "
    "diagrams, network diagrams, ray diagrams, graphs with plotted data or axes, labeled "
    "physical setups, geometric figures, flowcharts, and maps.\n\n"
    "This does NOT include: tables (even tables with borders), truth tables, mathematical "
    "equations or expressions, pseudocode, program code, text with unusual formatting, "
    "page decorations, logos, or page numbers. Don't include text lines beside the graphic.\n\n"
    "For each graphic return:\n"
    "  question_number — the question number as printed in the mark scheme (e.g. \"3(b)(ii)\")\n"
    "  bbox            — [x_min, y_min, x_max, y_max] as integers on a 0\u20131000 scale\n"
    "  description     — short label (e.g. \"circuit diagram\")\n\n"
    "Return an empty graphics list if the page has no graphics."
)

_SYSTEM_LAYOUT = "You are an expert at identifying exam paper printing layouts."

_USER_LAYOUT = """\
Look at this exam page image. Determine how many exam sub-pages are printed on this \
physical page and in what reading order they appear.

Return:
- "rows": number of rows of sub-pages (1 or 2)
- "cols": number of columns of sub-pages (1 or 2)
- "reading_order": list of [row, col] pairs (1-based) in the order a reader would \
read the sub-pages left-to-right, top-to-bottom

Standard single-page exam:
  {"rows":1,"cols":1,"reading_order":[[1,1]]}
Two-up landscape (left exam / right exam):
  {"rows":1,"cols":2,"reading_order":[[1,1],[1,2]]}
Four-up 2x2 grid, standard reading order:
  {"rows":2,"cols":2,"reading_order":[[1,1],[1,2],[2,1],[2,2]]}
"""
