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

    def system_exam_prompt(self) -> str:
        return load_prompt("parse_exam_pdf_yaml", section="system")[1]

    def system_scheme_prompt(self, is_cs: bool = False) -> str:
        from xscore.scaffold.scaffold_prompts import make_system_scheme_prompt
        return make_system_scheme_prompt("parse_mark_scheme_yaml", is_cs=is_cs)

    def build_exam_prompt(self, layout_result, is_split: bool, n_split_pages: int) -> str:
        return _build_user_exam_prompt_yaml(layout_result, is_split, n_split_pages)

    def build_scheme_user_msg(
        self, scaffold_str: str, page_num: int, n_pages: int,
        input_label: str = "PDF",
    ) -> str:
        page_note = (
            f"\n\nNote: the {input_label} you receive contains only page {page_num} of {n_pages} "
            "of the mark scheme. Only fill in `correct_answer` and `criteria` entries for "
            "questions whose criteria appear on this page. For all other questions leave "
            "`correct_answer` as `\"\"` and `criteria` as `[]`."
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
