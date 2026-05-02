"""Lightweight prompt loader — reads ``xscore/prompts/**/<name>.md``.

The file may begin with a YAML-style front-matter block delimited by ``---``
lines on their own. Recognised keys: ``version``, ``model_hint``,
``output_format``, ``description``. All keys are optional; ``version``
defaults to ``"v1"``.

Files may be combined (multiple roles per file) using Markdown H2 section
headers like ``## SYSTEM`` or ``## USER`` (case-insensitive). Pass
``section="system"`` (or ``"user"``, ``"field_rules"``, etc.) to extract a
specific section's body. Files without any ``## NAME`` header are returned
in full regardless of ``section``.

Substitution uses :class:`string.Template` semantics (``$placeholder``,
``${placeholder}``) via :meth:`safe_substitute` — missing placeholders are
left literal rather than raising. This keeps prompts that legitimately
contain ``$...`` (e.g. LaTeX math like ``$v = 2\\pi r / T$``) safe to load
even when no substitutions are passed.

Lookup is recursive: prompt files live in step-named subfolders
(e.g. ``ai_marking/ai_marking.md``) and callers reference them by bare
filename stem. Stems must be unique across all subfolders.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from string import Template

_PROMPTS_DIR = Path(__file__).resolve().parent

_SECTION_HEADER = re.compile(r'^## ([A-Z_][A-Z0-9_]*)\s*$', re.MULTILINE | re.IGNORECASE)


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Return ``(front_matter_dict, body)`` from a prompt file's contents."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, text
    lines = text.splitlines(keepends=True)
    end_idx = None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\r\n") == "---":
            end_idx = i
            break
    if end_idx is None:
        return {}, text
    fm_lines = lines[1:end_idx]
    body = "".join(lines[end_idx + 1 :])
    fm: dict[str, str] = {}
    for line in fm_lines:
        if ":" in line:
            k, v = line.split(":", 1)
            fm[k.strip()] = v.strip()
    return fm, body.lstrip("\n")


def _split_sections(body: str) -> dict[str, str]:
    """Split *body* by ``## NAME`` H2 markdown headers.

    Returns ``{name_lowercased: section_body}``. Each section's body extends
    from the line after its header to the line before the next header (or to
    EOF). The body is stripped of leading/trailing newlines but inner
    whitespace is preserved.

    Returns ``{}`` when no headers are present — caller falls back to the
    full body.
    """
    matches = list(_SECTION_HEADER.finditer(body))
    if not matches:
        return {}
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        name = m.group(1).lower()
        sections[name] = body[start:end].strip("\n")
    return sections


@lru_cache(maxsize=1)
def _path_index() -> dict[str, Path]:
    """Build ``{stem: path}`` mapping by walking ``_PROMPTS_DIR`` recursively.

    Stems must be unique across all subfolders. Raises :class:`RuntimeError`
    on a duplicate so the conflict surfaces at startup, not at call time.
    """
    index: dict[str, Path] = {}
    for path in sorted(_PROMPTS_DIR.rglob("*.md")):
        if path.stem in index:
            raise RuntimeError(
                f"Duplicate prompt name {path.stem!r}: "
                f"{index[path.stem].relative_to(_PROMPTS_DIR)} vs "
                f"{path.relative_to(_PROMPTS_DIR)}"
            )
        index[path.stem] = path
    return index


@lru_cache(maxsize=64)
def _load_raw(name: str) -> tuple[dict[str, str], str]:
    """Read and split a prompt file. Cached because prompts are immutable per-process."""
    paths = _path_index()
    path = paths.get(name)
    if path is None:
        raise FileNotFoundError(
            f"Prompt {name!r} not found under {_PROMPTS_DIR}. "
            f"Available prompts: {sorted(paths.keys())}"
        )
    return _split_front_matter(path.read_text(encoding="utf-8"))


def load_prompt(
    name: str, /, *, section: str | None = None, **substitutions: object,
) -> tuple[str, str]:
    """Return ``(version, body)`` for the named prompt.

    *section* (keyword-only) selects a specific ``## NAME`` block within a
    combined prompt file (``## SYSTEM``, ``## USER``, etc., case-insensitive).
    When the file has no section headers OR the requested section isn't
    present, the full body is returned.

    *substitutions* are applied via :class:`string.Template.safe_substitute`,
    so missing placeholders remain literal (no KeyError). This means a prompt
    can contain ``$something`` it wants preserved as long as the caller doesn't
    pass ``something=...``.
    """
    fm, body = _load_raw(name)
    if section is not None:
        sections = _split_sections(body)
        body = sections.get(section.lower(), body)
    if substitutions:
        body = Template(body).safe_substitute(
            {k: str(v) for k, v in substitutions.items()}
        )
    return fm.get("version", "v1"), body


def prompt_metadata(name: str) -> dict[str, str]:
    """Return the front-matter for a prompt without rendering its body.

    Useful for callers that want to log ``prompt_version`` alongside an artifact.
    """
    fm, _ = _load_raw(name)
    return dict(fm)


def all_prompt_versions() -> dict[str, str]:
    """Return ``{name: version}`` for every ``.md`` prompt found recursively
    under this package.

    Used by the run-manifest writer to pin which prompt versions a given
    pipeline run consumed.
    """
    out: dict[str, str] = {}
    for stem in sorted(_path_index().keys()):
        try:
            fm, _ = _load_raw(stem)
        except FileNotFoundError:
            continue
        out[stem] = fm.get("version", "v1")
    return out
