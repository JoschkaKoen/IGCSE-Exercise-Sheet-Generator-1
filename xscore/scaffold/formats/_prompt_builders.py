"""User-message prompt builders for the scaffold AI calls.

Two pairs of (entry-point, tail-template) builders:

- ``_build_user_question_numbers_prompt_yaml`` / ``_common_tail_scaffold_yaml``
  — step 19 (extract question numbers + structural metadata, no text/options).
- ``_build_user_exam_prompt_yaml`` / ``_common_tail_yaml`` — extract full
  question text + options.

Both branches handle the 1×1, n-up split, and pre-detected layouts in the
same way: layout-context preamble, then a YAML-shape tail with the right
per-question ``page`` / ``subpage_*`` descriptions.

Extracted from ``base.py`` so the format class isn't buried under prompt
literals.
"""

from __future__ import annotations


def _build_user_question_numbers_prompt_yaml(
    layout_result, is_split: bool, n_split_pages: int,
) -> str:
    """YAML-adapted extract-question-numbers user prompt — like
    ``_build_user_exam_prompt_yaml`` but the per-question schema in the tail
    drops ``text`` and ``options``."""
    _QUAD = {
        (1, 1): "top-left", (1, 2): "top-right",
        (2, 1): "bottom-left", (2, 2): "bottom-right",
    }

    if layout_result is None:
        layout_block = (
            "## Layout context\n"
            "Layout has not been pre-detected. Identify the page layout yourself "
            "and set `rows` and `cols` at the document root.\n"
            "- Standard single-page exam: `rows: 1`, `cols: 1`.\n"
            "- 4-up exam (2×2 grid per physical sheet): `rows: 2`, `cols: 2`.\n"
            "If a question spans more than one page, use the page on which "
            "its label first appears.\n"
        )
        page_desc      = "1-based page number where this question first appears"
        subpage_r_desc = "1-based row of the quadrant (1 for 1×1; 1=top, 2=bottom for 2×2)"
        subpage_c_desc = "1-based column of the quadrant (1 for 1×1; 1=left, 2=right for 2×2)"
    else:
        rows, cols = layout_result.rows, layout_result.cols
        if is_split:
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
                    f"  PDF page {split_p} → page: {phys}  subpage_row: {rc[0]}  "
                    f"subpage_col: {rc[1]}  ({label})"
                )
            mapping = "\n".join(mapping_lines)
            layout_block = (
                "## Layout context\n"
                f"The layout of this exam has already been detected: "
                f"{rows}×{cols} grid, reading order: {reading_order_str}.\n"
                f"This document has been pre-split into {n_split_pages} individual sub-pages "
                "(one per PDF page).\n"
                f"Set root keys: `rows: {rows}`  `cols: {cols}`.\n\n"
                "Use this mapping to set `page`, `subpage_row`, and `subpage_col` "
                "for each question, based on which PDF page the question is "
                "physically printed on:\n"
                f"{mapping}\n"
                "The same question number can appear more than once in the same "
                "quadrant; assign the quadrant each instance is physically in. "
                "If a question spans more than one PDF page, use the page on which "
                "its label first appears.\n"
            )
            page_desc      = "page number from the mapping above"
            subpage_r_desc = "subpage_row from the mapping above"
            subpage_c_desc = "subpage_col from the mapping above"
        else:
            layout_block = (
                "## Layout context\n"
                "The layout of this exam has already been detected: 1×1 "
                "(one sub-page per page).\n"
                "Set root keys: `rows: 1`  `cols: 1`.\n"
                "Set `subpage_row: 1` and `subpage_col: 1` for every question. "
                "If a question spans more than one page, use the page on which "
                "its label first appears.\n"
            )
            page_desc      = "1-based page number where this question first appears"
            subpage_r_desc = "always 1"
            subpage_c_desc = "always 1"

    output_block = (
        "## Output format\n"
        "Return ONLY well-formed YAML. No markdown fences in your response, "
        "no commentary outside the YAML document.\n"
    )

    return (
        layout_block + "\n"
        + output_block + "\n"
        + _common_tail_scaffold_yaml(page_desc, subpage_r_desc, subpage_c_desc)
    )


def _common_tail_scaffold_yaml(
    page_desc: str, subpage_r_desc: str, subpage_c_desc: str,
) -> str:
    return (
        "## Schema\n"
        "List every question and sub-question at every nesting level. "
        "Nested sub-questions go under `subquestions` of their parent.\n"
        "\n"
        "```yaml\n"
        "rows: <int>\n"
        "cols: <int>\n"
        "questions:\n"
        "  - number: \"9\"          # label as printed, run-together; no parentheses or spaces\n"
        "    type: short_answer    # multiple_choice | short_answer | calculation | long_answer\n"
        f"    page: <int>          # {page_desc}\n"
        f"    subpage_row: <int>   # {subpage_r_desc}\n"
        f"    subpage_col: <int>   # {subpage_c_desc}\n"
        "    marks: <int>          # see Constraints below\n"
        "    subquestions:\n"
        "      - number: \"9a\"\n"
        "        type: calculation\n"
        "        page: <int>\n"
        "        subpage_row: <int>\n"
        "        subpage_col: <int>\n"
        "        marks: <int>\n"
        "        subquestions:\n"
        "          - number: \"9ai\"\n"
        "            ...\n"
        "```\n"
        "\n"
        "## Constraints\n"
        "- **Do NOT include `text` or `options` keys.** Structural metadata only. "
        "(The fill phase produces those separately.)\n"
        "- `number` formatting — run-together, no parentheses, no spaces. "
        "Use lower-case Roman numerals for the third level: `9`, then `9a`, then `9ai` "
        "(not `9(a)(i)`, not `9.a.i`, not `9aI`).\n"
        "- `type` choice:\n"
        "  - `multiple_choice` — printed answer options A/B/C/D the candidate selects from.\n"
        "  - `calculation` — requires numeric working with units (physics, chemistry math, etc.).\n"
        "  - `long_answer` — extended prose, typically 4+ marks, no numeric answer.\n"
        "  - `short_answer` — everything else (one-word, one-line, define / name / state / identify).\n"
        "  When unsure between `short_answer` and `calculation`, prefer `calculation` if a "
        "numeric answer with units is expected; otherwise `short_answer`.\n"
        "- `marks` — integer mark allocation. Look for `[N]`, `[N marks]`, `(N marks)`, or "
        "`[Total: N]` printed near the question. Use the per-part allocation, not the question "
        "total. If no mark is printed, emit `0`.\n"
        "\n"
        "## Worked example\n"
        "A 2-page paper with one MCQ on page 1 and a 2-part calculation question on page 2:\n"
        "\n"
        "```yaml\n"
        "rows: 1\n"
        "cols: 1\n"
        "questions:\n"
        "  - number: \"1\"\n"
        "    type: multiple_choice\n"
        "    page: 1\n"
        "    subpage_row: 1\n"
        "    subpage_col: 1\n"
        "    marks: 1\n"
        "  - number: \"2\"\n"
        "    type: short_answer\n"
        "    page: 2\n"
        "    subpage_row: 1\n"
        "    subpage_col: 1\n"
        "    marks: 0\n"
        "    subquestions:\n"
        "      - number: \"2a\"\n"
        "        type: calculation\n"
        "        page: 2\n"
        "        subpage_row: 1\n"
        "        subpage_col: 1\n"
        "        marks: 3\n"
        "      - number: \"2b\"\n"
        "        type: calculation\n"
        "        page: 2\n"
        "        subpage_row: 1\n"
        "        subpage_col: 1\n"
        "        marks: 2\n"
        "```\n"
    )


def _build_user_exam_prompt_yaml(layout_result, is_split: bool, n_split_pages: int) -> str:
    """YAML-adapted version of _build_user_exam_prompt."""
    _QUAD = {
        (1, 1): "top-left", (1, 2): "top-right",
        (2, 1): "bottom-left", (2, 2): "bottom-right",
    }

    if layout_result is None:
        return (
            "Return ONLY well-formed YAML, no markdown fences or other text outside the YAML.\n\n"
            "Extract every question and sub-question as a YAML document with structure:\n"
            "  rows: <int>\n  cols: <int>\n  questions:\n    - number: ...\n      ...\n\n"
            + _common_tail_yaml("1-based page number", "1", "1")
        )

    rows, cols = layout_result.rows, layout_result.cols

    if is_split:
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
                f"  PDF page {split_p} → page: {phys}  subpage_row: {rc[0]}  "
                f"subpage_col: {rc[1]}  ({label})"
            )
        mapping = "\n".join(mapping_lines)
        header = (
            f"The layout of this exam has already been detected: "
            f"{rows}×{cols} grid, reading order: {reading_order_str}.\n"
            f"This PDF has been pre-split into {n_split_pages} individual sub-pages.\n\n"
            "Return ONLY well-formed YAML, no markdown fences or other text outside the YAML.\n\n"
            f"Set root keys rows: {rows}  cols: {cols}\n\n"
            "Use this mapping to set page, subpage_row, and subpage_col for each question:\n"
            f"{mapping}\n\n"
        )
        page_desc      = "page number from the mapping above"
        subpage_r_desc = "subpage_row from the mapping above"
        subpage_c_desc = "subpage_col from the mapping above"
    else:
        header = (
            "The layout of this exam has already been detected: 1×1.\n\n"
            "Return ONLY well-formed YAML, no markdown fences or other text outside the YAML.\n\n"
            "Set root keys rows: 1  cols: 1\n"
            "Set subpage_row: 1 and subpage_col: 1 for every question.\n\n"
        )
        page_desc      = "1-based page number where this question first appears"
        subpage_r_desc = "always 1"
        subpage_c_desc = "always 1"

    return header + _common_tail_yaml(page_desc, subpage_r_desc, subpage_c_desc)


def _common_tail_yaml(page_desc: str, subpage_r_desc: str, subpage_c_desc: str) -> str:
    return (
        "Extract every question and sub-question at every nesting level.\n"
        "Use this YAML structure:\n"
        "  rows: <int>\n"
        "  cols: <int>\n"
        "  questions:\n"
        "    - number: \"9\"          # label as printed, run-together; no parentheses\n"
        "      type: short_answer    # multiple_choice | short_answer | calculation | long_answer\n"
        f"      page: <int>          # {page_desc}\n"
        f"      subpage_row: <int>   # {subpage_r_desc}\n"
        f"      subpage_col: <int>   # {subpage_c_desc}\n"
        "      marks: <int>          # integer from [N] brackets; 0 if not printed\n"
        "      text: |               # complete question text; $...$ for math\n"
        "        Question text here.\n"
        "      options:              # for multiple_choice only\n"
        "        - letter: A\n"
        "          text: option text\n"
        "      subquestions:\n"
        "        - number: \"9a\"\n"
        "          ...\n"
        "Nested sub-questions go under `subquestions` of their parent.\n"
        "Use block scalars (`|`) for `text` fields.\n"
    )
