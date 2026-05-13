"""YAML I/O for the scaffold format.

A custom :class:`yaml.SafeDumper` subclass that emits multiline strings as
block scalars (so LaTeX backslashes survive a round-trip), plus a recovering
load helper that patches two known model-output failure modes before
re-parsing:

- unquoted ``: `` mid-scalar (e.g. ratios like ``18 (: 1)`` that PyYAML reads
  as a mapping value),
- double-quoted scalars containing LaTeX backslashes that YAML rejects as
  unknown escape sequences (``\\newline``, ``\\leftarrow``).

Extracted from ``base.py`` so the format class isn't buried under YAML
boilerplate.
"""

from __future__ import annotations

import re

import yaml

from xscore.shared.terminal_ui import warn_line


# ---------------------------------------------------------------------------
# Custom dumper — block scalars for multiline strings with backslashes
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


# ---------------------------------------------------------------------------
# Recovery patterns + per-line patchers
# ---------------------------------------------------------------------------

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
