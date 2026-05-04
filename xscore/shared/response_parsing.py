"""Shared utilities for parsing and cleaning AI response text."""

from __future__ import annotations

import json
import re
from typing import Any


def strip_code_fences(raw: str) -> str:
    """Strip ``` code fences from a response string.

    Removes one optional leading fence (``` or ```lang) and one optional
    trailing fence. The fences must be on their own around the content.
    Idempotent on already-stripped input.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[^\n]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())
    return raw


_ALLTT_OPEN_RE = re.compile(r"^(\s+)\\begin\{alltt\}\s*$")
_ALLTT_CLOSE_RE = re.compile(r"^\s*\\end\{alltt\}\s*$")
_BLOCK_OPEN_RE = re.compile(r":\s*[|>][+-]?(\d?)\s*$")


def repair_block_scalar_first_line_indent(raw: str) -> str:
    """Strip excess indent from the first content line of a block scalar.

    PyYAML uses the first non-empty body line of a ``|`` (or ``>``) block to
    set the indentation baseline; any later line indented less than that
    baseline terminates the scalar. Some models add a leading space to the
    first content line — typically because the student's handwriting began
    with a visible gap on the dotted line — leaving subsequent lines below
    the baseline and breaking the parse. This pass detects that pattern and
    dedents the first body line to match the minimum indent of the rest.

    Idempotent on already-valid content: after one pass ``first_indent ==
    min_rest`` so the rule never fires again. Skips blocks with explicit
    indent indicators (``|2``, ``|2-`` …) and bails on any block whose body
    has a tab in its leading whitespace.
    """
    lines = raw.splitlines()
    i = 0
    while i < len(lines):
        m = _BLOCK_OPEN_RE.search(lines[i])
        if not m or m.group(1):  # no opener, or explicit indent indicator
            i += 1
            continue
        opener_indent = len(lines[i]) - len(lines[i].lstrip(" "))
        end = i + 1
        while end < len(lines):
            line = lines[end]
            if not line.strip():
                end += 1
                continue
            line_indent = len(line) - len(line.lstrip(" "))
            if line_indent <= opener_indent:
                break
            end += 1
        body_idxs = [j for j in range(i + 1, end) if lines[j].strip()]
        if len(body_idxs) < 2:
            i = end
            continue
        if any(lines[j].lstrip(" ").startswith("\t") for j in body_idxs):
            i = end
            continue
        first = body_idxs[0]
        first_indent = len(lines[first]) - len(lines[first].lstrip(" "))
        min_rest = min(
            len(lines[j]) - len(lines[j].lstrip(" ")) for j in body_idxs[1:]
        )
        if first_indent > min_rest:
            lines[first] = lines[first][first_indent - min_rest:]
        i = end
    return "\n".join(lines) + ("\n" if raw.endswith("\n") else "")


def repair_alltt_block_indent(raw: str) -> str:
    """Re-indent dedented alltt code blocks inside YAML block scalars.

    Some models emit ``\\begin{alltt}`` correctly indented inside a
    ``student_answer: |`` block but flush the code lines inside to column 0,
    terminating the YAML block scalar early and breaking the parse. This
    pass detects each ``\\begin{alltt}…\\end{alltt}`` pair where the body
    is dedented below the opener and shifts the body up to match,
    preserving any relative indentation. Idempotent on already-valid
    content.
    """
    lines = raw.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _ALLTT_OPEN_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        opener_indent = len(m.group(1))
        end_idx: int | None = None
        for j in range(i + 1, len(lines)):
            if _ALLTT_CLOSE_RE.match(lines[j]):
                end_idx = j
                break
            if _ALLTT_OPEN_RE.match(lines[j]):
                end_idx = None
                break
        if end_idx is None:
            out.append(lines[i])
            i += 1
            continue
        body = lines[i + 1:end_idx]
        body_indents = [len(s) - len(s.lstrip(" ")) for s in body if s.strip()]
        if body_indents and min(body_indents) < opener_indent:
            shift = opener_indent - min(body_indents)
            pad = " " * shift
            shifted_body = [pad + s if s.strip() else s for s in body]
            out.append(lines[i])
            out.extend(shifted_body)
            out.append(" " * opener_indent + r"\end{alltt}")
        else:
            out.extend(lines[i:end_idx + 1])
        i = end_idx + 1
    return "\n".join(out) + ("\n" if raw.endswith("\n") else "")


def parse_json_safe(raw: str) -> dict | None:
    """Parse JSON from model text; slice object bounds; light truncation repair.

    Returns the parsed dict on success (including an empty ``{}`` if the model
    genuinely returned one), or ``None`` if the text could not be parsed as a
    JSON object at all.  Callers should check ``if result is not None`` rather
    than ``if result`` to avoid treating a valid empty dict as a parse failure.
    """
    text = raw.strip()
    if not text:
        return None

    def _as_dict(obj: Any) -> dict | None:
        return obj if isinstance(obj, dict) else None

    try:
        result = _as_dict(json.loads(text))
        if result is not None:
            return result
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            result = _as_dict(json.loads(text[start : end + 1]))
            if result is not None:
                return result
        except json.JSONDecodeError:
            pass

    try:
        fixed = text
        if fixed.count('"') % 2 == 1:
            fixed = fixed.rstrip() + '"}'
        if not fixed.rstrip().endswith("}"):
            fixed = fixed.rstrip() + "}"
        result = _as_dict(json.loads(fixed))
        if result is not None:
            return result
    except json.JSONDecodeError:
        pass

    try:
        cleaned = re.sub(r'[\x00-\x1f]', lambda m: '\\u{:04x}'.format(ord(m.group())), text)
        result = _as_dict(json.loads(cleaned))
        if result is not None:
            return result
    except json.JSONDecodeError:
        pass

    return None
