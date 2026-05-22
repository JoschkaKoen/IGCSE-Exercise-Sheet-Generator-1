"""Lightweight prompt loader for eXam.

Mirrors xscore/prompts/loader.py shape but rooted at ``eXam/prompts/``.
Skips the ``$include_`` fragment mechanism intentionally (per project memory:
no shared prompt fragments — inline rules in each prompt).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from string import Template

_PROMPTS_DIR = Path(__file__).resolve().parent
_SECTION_HEADER = re.compile(r"^## ([A-Z_][A-Z0-9_]*)\s*$", re.MULTILINE)


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
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
    matches = list(_SECTION_HEADER.finditer(body))
    if not matches:
        return {}
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out[m.group(1).lower()] = body[start:end].strip("\n")
    return out


@lru_cache(maxsize=64)
def _load_raw(name: str) -> tuple[dict[str, str], str]:
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(
            f"Prompt {name!r} not found under {_PROMPTS_DIR}"
        )
    return _split_front_matter(path.read_text(encoding="utf-8"))


def load_prompt(
    name: str, /, *, section: str | None = None, **substitutions: object,
) -> tuple[str, str]:
    fm, body = _load_raw(name)
    if section is not None:
        sections = _split_sections(body)
        body = sections.get(section.lower(), body)
    if substitutions:
        body = Template(body).safe_substitute(
            {k: str(v) for k, v in substitutions.items()}
        )
    return fm.get("version", "v1"), body
