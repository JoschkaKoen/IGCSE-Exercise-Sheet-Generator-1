# -*- coding: utf-8 -*-
"""Loader for the Code page's authored Python lessons.

Lessons are committed source under ``content/code/<course>/`` (NOT under
``output/``, which is gitignored). Each course has a ``course.yaml`` manifest,
and per lesson ``NN``:

  - ``NN.en.md`` / ``NN.zh.md`` — bilingual prose (markdown; the route renders
    it with ``render_helper_markdown``, which preserves ``$…$`` for KaTeX).
  - ``NN.meta.yaml`` — lesson title plus the task list. Each task has a
    ``prompt`` (bilingual), ``starter`` code, an optional scripted ``stdin``
    (used by the deterministic check, never for live input), and a client-side
    ``check`` spec (``{kind: stdout, expected, normalize}`` or
    ``{kind: asserts, code}``).

This mirrors the authored-markdown-plus-YAML-sidecar pattern of the handouts
system (``web/handouts_collect.py``). The web route reads these at request time
— no in-process cache, like the Learn page.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

# ``web/`` sits at the repo root, so parents[1] is the repo root.
CODE_DIR = Path(__file__).resolve().parents[1] / "content" / "code"


def _pick(value: Any, lang: str) -> str:
    """Resolve a possibly-bilingual value to ``lang`` with an English fallback."""
    if isinstance(value, dict):
        return str(value.get(lang) or value.get("en") or next(iter(value.values()), ""))
    return str(value or "")


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def course_dir(slug: str) -> Path:
    return CODE_DIR / slug


def list_courses(lang: str) -> list[dict[str, Any]]:
    """Every course (each as returned by :func:`load_course`), sorted by slug."""
    if not CODE_DIR.is_dir():
        return []
    courses: list[dict[str, Any]] = []
    for child in sorted(CODE_DIR.iterdir()):
        if (child / "course.yaml").is_file():
            course = load_course(child.name, lang)
            if course:
                courses.append(course)
    return courses


def load_course(slug: str, lang: str) -> dict[str, Any] | None:
    """Course manifest + each lesson's number/title, resolved to ``lang``."""
    cdir = course_dir(slug)
    manifest = _read_yaml(cdir / "course.yaml")
    if not manifest:
        return None
    lessons: list[dict[str, str]] = []
    for nn in manifest.get("lessons") or []:
        nn = str(nn)
        meta = _read_yaml(cdir / f"{nn}.meta.yaml")
        if not meta:
            continue
        lessons.append({"nn": nn, "title": _pick(meta.get("title"), lang)})
    return {
        "slug": slug,
        "title": _pick(manifest.get("title"), lang),
        "subtitle": _pick(manifest.get("subtitle"), lang),
        # Runtime selector for the playground: "python" (Pyodide) or "java".
        # Absent → "python" so every existing course keeps working unchanged.
        "language": str(manifest.get("language") or "python"),
        "lessons": lessons,
    }


def load_lesson(slug: str, nn: str, lang: str) -> dict[str, Any] | None:
    """One lesson resolved to ``lang`` (English fallback). ``None`` if missing.

    Returns the prose markdown (for the route to render), the per-task prompts
    (for server-side rendering), and ``tasks_client_json`` — the JSON the
    browser module needs to run and check each task (id, starter, stdin, check).
    """
    cdir = course_dir(slug)
    meta = _read_yaml(cdir / f"{nn}.meta.yaml")
    if not meta:
        return None

    prose_path = cdir / f"{nn}.{lang}.md"
    if not prose_path.is_file():
        prose_path = cdir / f"{nn}.en.md"
    try:
        prose_md = prose_path.read_text(encoding="utf-8")
    except OSError:
        prose_md = ""

    # Split the prose into step chunks on thematic-break lines (`---`). Split the
    # RAW markdown before rendering, so a "text\n---" sequence can't be parsed as
    # a setext <h2>. A lesson with no `---` is simply one chunk.
    prose_chunks = [c.strip() for c in re.split(r"(?m)^\s*-{3,}\s*$", prose_md) if c.strip()]

    tasks: list[dict[str, Any]] = []
    client: list[dict[str, Any]] = []
    for task in meta.get("tasks") or []:
        if not isinstance(task, dict) or not task.get("id"):
            continue
        tid = str(task["id"])
        starter = str(task.get("starter") or "")
        tasks.append({
            "id": tid,
            "prompt_md": _pick(task.get("prompt"), lang),
            "starter": starter,
        })
        client.append({
            "id": tid,
            "starter": starter,
            "stdin": str(task.get("stdin") or ""),
            "check": task.get("check") if isinstance(task.get("check"), dict) else {},
        })

    # Previous / next lesson within the course, for footer navigation.
    course = load_course(slug, lang)
    order = [lesson["nn"] for lesson in (course or {}).get("lessons", [])]
    prev_nn = next_nn = None
    if nn in order:
        idx = order.index(nn)
        prev_nn = order[idx - 1] if idx > 0 else None
        next_nn = order[idx + 1] if idx < len(order) - 1 else None

    return {
        "slug": slug,
        "nn": nn,
        "title": _pick(meta.get("title"), lang),
        "course_title": (course or {}).get("title", slug),
        # Per-lesson override wins, else the course default, else "python".
        "language": str(meta.get("language") or (course or {}).get("language") or "python"),
        "prose_md": prose_md,
        "prose_chunks": prose_chunks,
        "tasks": tasks,
        "tasks_client_json": json.dumps(client, ensure_ascii=False),
        "prev": prev_nn,
        "next": next_nn,
    }
