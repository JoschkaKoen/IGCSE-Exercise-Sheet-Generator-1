"""YAML scaffold format — block scalars for zero LaTeX escaping in criteria."""

from __future__ import annotations


import re
import yaml

from xscore.prompts.loader import load_prompt
from xscore.scaffold.formats.base import ScaffoldFormat
from xscore.shared.terminal_ui import warn_line


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ScaffoldDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.ScalarNode:
    if "\n" in data or "\\" in data:
        # Strip per-line trailing whitespace so PyYAML can use block-scalar
        # style. Without this, multiline strings with trailing whitespace fall
        # back to double-quoted form, which interprets backslashes as escapes
        # and silently destroys LaTeX commands.
        data = "\n".join(line.rstrip() for line in data.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_ScaffoldDumper.add_representer(str, _str_representer)


from xscore.shared.response_parsing import strip_code_fences as _strip_fences  # noqa: E402


# Used by _load_scheme_yaml_recovering: matches lines like "  key: value" where
# the value is an unquoted plain scalar. The recovery quotes the value when
# PyYAML rejects it for containing ": " mid-scalar (e.g. ratios like "18 (: 1)").
_PLAIN_KV_RE = re.compile(r"^(\s+)([\w-]+):\s(.+)$")
_QUOTE_INDICATORS = ("'", '"', "|", ">", "[", "{", "&", "*", "!")

# Matches a single-line "  key: \"value\"" pair. Used by the recovery branch for
# `found unknown escape character` errors — converts the double-quoted scalar to
# a single-quoted scalar so LaTeX backslashes are preserved literally.
_DQ_KV_RE = re.compile(r'^(\s+[\w-]+:\s+)"(.*)"\s*$')


def _quote_unquoted_value(line: str) -> "str | None":
    m = _PLAIN_KV_RE.match(line)
    if not m:
        return None
    indent, key, value = m.group(1), m.group(2), m.group(3)
    value = value.rstrip()
    if not value or value.startswith(_QUOTE_INDICATORS):
        return None
    escaped = value.replace("'", "''")
    return f"{indent}{key}: '{escaped}'"


def _double_quoted_to_single(line: str) -> "str | None":
    """Convert ``key: "value"`` to ``key: 'value'`` on a single line, preserving
    backslashes literally (single-quoted YAML scalars don't process escapes).
    Returns ``None`` if the line isn't a single-line ``key: "..."`` pair.
    """
    m = _DQ_KV_RE.match(line)
    if not m:
        return None
    prefix, content = m.group(1), m.group(2)
    # Inside a YAML double-quoted scalar, \" is the only way to embed ";
    # in single-quoted form, " is literal — undo that escape.
    content = content.replace('\\"', '"')
    # Single-quoted YAML escapes ' as ''.
    content = content.replace("'", "''")
    return f"{prefix}'{content}'"


def _load_scheme_yaml_recovering(text: str):
    """Parse YAML, recovering from two known model-output failure modes:
    (1) unquoted ``: `` mid-scalar (e.g. ratios like ``18 (: 1)``) — quote the
        offending line; (2) double-quoted scalars containing LaTeX backslashes
        (``\\newline``, ``\\leftarrow``) that YAML rejects as unknown escape
        sequences — convert to a single-quoted scalar that preserves backslashes
        literally. Raises ``RuntimeError`` if the input cannot be parsed even
        after recovery. Empty/None content is *not* an error — returns whatever
        ``yaml.safe_load`` produces (``None`` for empty input).
    """
    patched: set[int] = set()
    for _ in range(5):
        try:
            return yaml.safe_load(text)
        except yaml.MarkedYAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            problem = getattr(exc, "problem", "") or ""
            if mark is None:
                raise RuntimeError(f"Mark scheme YAML parse error: {exc}") from exc
            line_idx = mark.line
            if line_idx in patched:
                raise RuntimeError(f"Mark scheme YAML parse error: {exc}") from exc
            lines = text.split("\n")
            if not 0 <= line_idx < len(lines):
                raise RuntimeError(f"Mark scheme YAML parse error: {exc}") from exc
            original = lines[line_idx]

            if "mapping values are not allowed" in problem:
                patched_line = _quote_unquoted_value(original)
                recovery_label = "unquoted ':'"
            elif "found unknown escape character" in problem:
                patched_line = _double_quoted_to_single(original)
                recovery_label = "double-quoted scalar with LaTeX backslash"
            else:
                raise RuntimeError(f"Mark scheme YAML parse error: {exc}") from exc

            if patched_line is None:
                raise RuntimeError(f"Mark scheme YAML parse error: {exc}") from exc
            warn_line(
                f"Mark scheme YAML: recovered from {recovery_label} on line "
                f"{line_idx + 1}: {original.strip()!r}"
            )
            lines[line_idx] = patched_line
            text = "\n".join(lines)
            patched.add(line_idx)
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Mark scheme YAML parse error: {exc}") from exc
    raise RuntimeError("Mark scheme YAML parse error: too many recovery attempts")


def _build_user_scaffold_prompt_yaml(
    layout_result, is_split: bool, n_split_pages: int,
) -> str:
    """YAML-adapted detect-scaffold user prompt — like
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
            f"{rows}\u00d7{cols} grid, reading order: {reading_order_str}.\n"
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
            "The layout of this exam has already been detected: 1\u00d71.\n\n"
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


class YamlScaffoldFormat(ScaffoldFormat):

    def system_exam_prompt(self, is_cs: bool = False) -> str:
        from xscore.scaffold.scaffold_prompts import make_system_exam_prompt
        return make_system_exam_prompt("parse_exam_pdf_yaml", is_cs=is_cs)

    def system_scheme_prompt(self, is_cs: bool = False) -> str:
        from xscore.scaffold.scaffold_prompts import make_system_scheme_prompt
        return make_system_scheme_prompt("parse_mark_scheme_yaml", is_cs=is_cs)

    def system_scaffold_prompt(self, is_cs: bool = False) -> str:
        from xscore.scaffold.scaffold_prompts import make_system_scaffold_prompt
        return make_system_scaffold_prompt("detect_exam_scaffold_yaml", is_cs=is_cs)

    def system_fill_prompt(self, is_cs: bool = False) -> str:
        from xscore.scaffold.scaffold_prompts import make_system_fill_prompt
        return make_system_fill_prompt("fill_exam_scaffold_yaml", is_cs=is_cs)

    def build_exam_prompt(self, layout_result, is_split: bool, n_split_pages: int) -> str:
        return _build_user_exam_prompt_yaml(layout_result, is_split, n_split_pages)

    def build_scheme_user_msg(
        self, scaffold_str: str, page_num: int, n_pages: int,
        input_label: str = "PDF",
    ) -> str:
        page_note = (
            f"\n\n## Page context\n"
            f"The {input_label} you receive is page {page_num} of {n_pages} of the mark scheme.\n"
            f"Fill `correct_answer` and `criteria` for the questions whose criteria appear on this page. "
            f'For every other question in the scaffold, leave `correct_answer: ""` and `criteria: []`.\n'
            f"Keep every scaffold entry — do not remove any."
        )
        return load_prompt(
            "parse_mark_scheme_yaml", section="user", scaffold=scaffold_str,
        )[1] + page_note

    def build_scheme_scaffold(self, questions: list[dict]) -> str:
        """Build YAML scaffold from exam questions for the scheme AI."""
        entries = []

        def _visit(node: dict) -> None:
            entries.append({
                "number": str(node.get("number", "")),
                "type": str(node.get("question_type", "")),
                "marks": int(node.get("marks", 0)),
                "correct_answer": "",
                "criteria": [],
            })
            for sub in (node.get("subquestions") or []):
                _visit(sub)

        for q in questions:
            _visit(q)

        doc = {"questions": entries}
        return yaml.dump(
            doc, Dumper=_ScaffoldDumper,
            allow_unicode=True, default_flow_style=False,
            sort_keys=False,
        )

    def extract_question_numbers(self, scaffold_str: str) -> list[str]:
        try:
            data = yaml.safe_load(scaffold_str)
            if not isinstance(data, dict):
                return []
            return [
                str(q.get("number", ""))
                for q in data.get("questions", [])
                if isinstance(q, dict) and q.get("number")
            ]
        except yaml.YAMLError:
            return []

    def parse_exam_response(self, raw: str) -> tuple[list[dict], dict]:
        try:
            data = yaml.safe_load(_strip_fences(raw))
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Exam YAML parse error: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"Exam YAML: expected a mapping, got {type(data).__name__}")
        layout = {
            "rows": int(data.get("rows", 1)),
            "cols": int(data.get("cols", 1)),
        }
        questions = [_parse_yaml_question(q) for q in data.get("questions", []) if isinstance(q, dict)]
        return questions, layout

    def parse_scheme_response(self, raw: str) -> dict:
        data = _load_scheme_yaml_recovering(_strip_fences(raw))
        if not isinstance(data, dict):
            return {"questions": []}
        questions = []
        for q in data.get("questions", []):
            if not isinstance(q, dict):
                continue
            questions.append({
                "number":         str(q.get("number", "")),
                # str() wrap: model occasionally emits unquoted YAML int (e.g. `correct_answer: 5`); parser must coerce.
                "correct_answer": str(q.get("correct_answer") or "").strip() or None,
                "mark_scheme": [
                    {"mark": str(c.get("mark", "")), "criterion": str(c.get("criterion", "")).strip()}
                    for c in (q.get("criteria") or [])
                    if isinstance(c, dict)
                ],
                "graphics": [],
            })
        return {"questions": questions}

    def serialize_exam(self, questions: list[dict], layout: dict) -> str:
        doc = {
            "rows": layout.get("rows", 1),
            "cols": layout.get("cols", 1),
            "questions": [_exam_q_to_yaml_dict(q) for q in questions],
        }
        return yaml.dump(
            doc, Dumper=_ScaffoldDumper,
            allow_unicode=True, default_flow_style=False,
            sort_keys=False,
        )

    # ---- detect-scaffold (phase A) -----------------------------------------

    def build_scaffold_user_msg(
        self, layout_result, is_split: bool, n_split_pages: int,
    ) -> str:
        return _build_user_scaffold_prompt_yaml(layout_result, is_split, n_split_pages)

    def parse_scaffold_response(self, raw: str) -> tuple[list[dict], dict]:
        try:
            data = yaml.safe_load(_strip_fences(raw))
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Scaffold YAML parse error: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError(
                f"Scaffold YAML: expected a mapping, got {type(data).__name__}"
            )
        layout = {
            "rows": int(data.get("rows", 1)),
            "cols": int(data.get("cols", 1)),
        }
        nodes = [
            _parse_yaml_scaffold_node(q)
            for q in data.get("questions", [])
            if isinstance(q, dict)
        ]
        return nodes, layout

    def serialize_scaffold(self, nodes: list[dict], layout: dict) -> str:
        doc = {
            "rows": layout.get("rows", 1),
            "cols": layout.get("cols", 1),
            "questions": [_scaffold_node_to_yaml_dict(n) for n in nodes],
        }
        return yaml.dump(
            doc, Dumper=_ScaffoldDumper,
            allow_unicode=True, default_flow_style=False,
            sort_keys=False,
        )

    # ---- fill (phase B) -----------------------------------------------------

    def build_fill_stub(self, filtered_nodes: list[dict]) -> str:
        lines = []
        for n in filtered_nodes:
            num = str(n.get("number", ""))
            qt = str(n.get("question_type", "short_answer"))
            lines.append(f'  - number: "{num}"')
            lines.append(f"    type: {qt}")
            lines.append("    text: \"\"")
        return "\n".join(lines)

    def build_fill_user_msg(
        self, stub_str: str, page_num: int, n_pages: int,
        expected_qnums: list[str], input_label: str = "PDF",
    ) -> str:
        qnums_str = ", ".join(f'"{q}"' for q in expected_qnums) or "(none)"
        page_note = (
            f"\n\n## Page context\n"
            f"The {input_label} you receive is page {page_num} of {n_pages} of the exam. "
            f"The expected question numbers on this page are: {qnums_str}.\n"
            f"Return exactly these entries, in this order, one per `number` listed above. "
            f"Do not add, remove, reorder, or rename any entry."
        )
        return load_prompt(
            "fill_exam_scaffold_yaml", section="user", scaffold=stub_str,
        )[1] + page_note

    def parse_fill_response(self, raw: str) -> list[dict]:
        try:
            data = yaml.safe_load(_strip_fences(raw))
        except yaml.YAMLError as exc:
            raise RuntimeError(f"Fill YAML parse error: {exc}") from exc
        if isinstance(data, dict):
            entries = data.get("questions") or []
        elif isinstance(data, list):
            entries = data
        else:
            return []
        out: list[dict] = []
        for q in entries:
            if not isinstance(q, dict):
                continue
            out.append({
                "number":  str(q.get("number", "")),
                "text":    str(q.get("text", "")).strip(),
                "options": [
                    {"letter": str(o.get("letter", "")), "text": str(o.get("text", "")).strip()}
                    for o in (q.get("options") or [])
                    if isinstance(o, dict)
                ],
            })
        return out

    def artifact_ext(self) -> str:
        return "yaml"


# ---------------------------------------------------------------------------
# Helpers for parse / serialize
# ---------------------------------------------------------------------------

def _parse_yaml_question(q: dict) -> dict:
    return {
        "number":        str(q.get("number", "")),
        "question_type": str(q.get("type", "short_answer")),
        "page":          int(q.get("page", 1)),
        "subpage_row":   int(q.get("subpage_row", 1)),
        "subpage_col":   int(q.get("subpage_col", 1)),
        "marks":         int(q.get("marks", 0)),
        "text":          str(q.get("text", "")).strip(),
        "answer_options": [
            {"letter": str(o.get("letter", "")), "text": str(o.get("text", "")).strip()}
            for o in (q.get("options") or [])
            if isinstance(o, dict)
        ],
        "subquestions": [
            _parse_yaml_question(s) for s in (q.get("subquestions") or [])
            if isinstance(s, dict)
        ],
    }


def _parse_yaml_scaffold_node(q: dict) -> dict:
    """Parse a detect-scaffold YAML node — same shape as the exam parser but
    text/options are forced empty (the model is instructed not to emit them;
    this defends against accidental emission)."""
    return {
        "number":        str(q.get("number", "")),
        "question_type": str(q.get("type", "short_answer")),
        "page":          int(q.get("page", 1)),
        "subpage_row":   int(q.get("subpage_row", 1)),
        "subpage_col":   int(q.get("subpage_col", 1)),
        "marks":         int(q.get("marks", 0)),
        "text":          "",
        "answer_options": [],
        "subquestions": [
            _parse_yaml_scaffold_node(s) for s in (q.get("subquestions") or [])
            if isinstance(s, dict)
        ],
    }


def _scaffold_node_to_yaml_dict(q: dict) -> dict:
    """Serialise a scaffold node — drops text/options for a clean artifact."""
    entry: dict = {
        "number":      str(q.get("number", "")),
        "type":        str(q.get("question_type", "short_answer")),
        "page":        int(q.get("page", 1)),
        "subpage_row": int(q.get("subpage_row", 1)),
        "subpage_col": int(q.get("subpage_col", 1)),
        "marks":       int(q.get("marks", 0)),
    }
    subs = q.get("subquestions") or []
    if subs:
        entry["subquestions"] = [_scaffold_node_to_yaml_dict(s) for s in subs]
    return entry


def _exam_q_to_yaml_dict(q: dict) -> dict:
    entry: dict = {
        "number":      str(q.get("number", "")),
        "type":        str(q.get("question_type", "short_answer")),
        "page":        int(q.get("page", 1)),
        "subpage_row": int(q.get("subpage_row", 1)),
        "subpage_col": int(q.get("subpage_col", 1)),
        "marks":       int(q.get("marks", 0)),
        "text":        str(q.get("text", "")),
    }
    opts = q.get("answer_options") or []
    if opts:
        entry["options"] = [
            {"letter": str(o.get("letter", "")), "text": str(o.get("text", ""))}
            for o in opts
        ]
    subs = q.get("subquestions") or []
    if subs:
        entry["subquestions"] = [_exam_q_to_yaml_dict(s) for s in subs]
    return entry
