"""Lightweight prompt loader — reads ``xscore/prompts/<name>.md``.

The file may begin with a YAML-style front-matter block delimited by ``---``
lines on their own. Recognised keys: ``version``, ``model_hint``,
``output_format``, ``description``. All keys are optional; ``version``
defaults to ``"v1"``.

Substitution uses :class:`string.Template` semantics (``$placeholder``,
``${placeholder}``) via :meth:`safe_substitute` — missing placeholders are
left literal rather than raising. This keeps prompts that legitimately
contain ``{...}`` (e.g. JSON snippets in examples) safe to load even when
no substitutions are passed.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template

_PROMPTS_DIR = Path(__file__).resolve().parent


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


@lru_cache(maxsize=64)
def _load_raw(name: str) -> tuple[dict[str, str], str]:
    """Read and split a prompt file. Cached because prompts are immutable per-process."""
    path = _PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"Prompt {name!r} not found at {path}. "
            f"Available prompts: {sorted(p.stem for p in _PROMPTS_DIR.glob('*.md'))}"
        )
    return _split_front_matter(path.read_text(encoding="utf-8"))


def load_prompt(name: str, /, **substitutions: object) -> tuple[str, str]:
    """Return ``(version, body)`` for the named prompt.

    *substitutions* are applied via :class:`string.Template.safe_substitute`,
    so missing placeholders remain literal (no KeyError). This means a prompt
    can contain ``$something`` it wants preserved as long as the caller doesn't
    pass ``something=...``.
    """
    fm, body = _load_raw(name)
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
    """Return ``{name: version}`` for every ``.md`` prompt in this package.

    Used by the run-manifest writer (item 8) to pin which prompt versions a
    given pipeline run consumed.
    """
    out: dict[str, str] = {}
    for path in sorted(_PROMPTS_DIR.glob("*.md")):
        try:
            fm, _ = _load_raw(path.stem)
        except FileNotFoundError:
            continue
        out[path.stem] = fm.get("version", "v1")
    return out
