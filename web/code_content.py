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

from .content_cache import mtime_cached

# ``web/`` sits at the repo root, so parents[1] is the repo root.
CODE_DIR = Path(__file__).resolve().parents[1] / "content" / "code"

# Curriculum order for the landing page: the A-Level / foundation track first,
# easiest → hardest (Python Basics, then A-Level CS), then the AP track,
# easiest → hardest (CS Principles, then CS A / Java). Any course not listed here
# sorts after the curated ones (by slug), so a newly-added course is always shown
# — just uncurated — rather than dropped.
COURSE_ORDER = ("python-basics", "a-level-cs", "sql-databases", "ap-csp", "ap-csa")


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


def resolve_seed(slug: str, meta: dict[str, Any], task: dict[str, Any]) -> str:
    """Resolve a SQL task's seed script (the CREATE + INSERT run before the
    student's code). Precedence: task ``seed`` > task ``dataset`` > lesson ``seed``
    > lesson ``dataset`` (the latter two on ``meta``). ``dataset: <name>`` names
    ``datasets/<name>.sql`` under the course dir. Returns ``""`` when none is
    declared — so non-SQL courses simply get no ``seed`` field."""
    for src in (task, meta):
        if not isinstance(src, dict):
            continue
        if src.get("seed") is not None:
            return str(src["seed"])
        name = src.get("dataset")
        if name:
            try:
                return (course_dir(slug) / "datasets" / f"{name}.sql").read_text(encoding="utf-8")
            except OSError:
                return ""
    return ""


def _course_sort_key(path: Path) -> tuple[int, str]:
    """Curriculum rank for a course dir: its index in :data:`COURSE_ORDER`,
    else after all curated courses, tie-broken alphabetically by slug."""
    try:
        return (COURSE_ORDER.index(path.name), "")
    except ValueError:
        return (len(COURSE_ORDER), path.name)


def list_courses(lang: str) -> list[dict[str, Any]]:
    """Every course (each as returned by :func:`load_course`), in curriculum
    order (see :data:`COURSE_ORDER`)."""
    if not CODE_DIR.is_dir():
        return []
    children = [c for c in CODE_DIR.iterdir() if (c / "course.yaml").is_file()]
    courses: list[dict[str, Any]] = []
    for child in sorted(children, key=_course_sort_key):
        course = load_course(child.name, lang)
        if course:
            courses.append(course)
    return courses


@mtime_cached(lambda slug, lang: [course_dir(slug) / "course.yaml", *sorted(course_dir(slug).glob("*.meta.yaml"))])
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


@mtime_cached(lambda slug, nn, lang: [
    course_dir(slug) / f"{nn}.meta.yaml",
    course_dir(slug) / f"{nn}.{lang}.md",
    course_dir(slug) / f"{nn}.en.md",
    course_dir(slug) / "course.yaml",
    # SQL courses seed from shared datasets/*.sql — editing one must bust the cache.
    *sorted((course_dir(slug) / "datasets").glob("*.sql")),
])
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
        entry = {
            "id": tid,
            "starter": starter,
            "stdin": str(task.get("stdin") or ""),
            # Optional read-only support files (Java: extra .java compiled alongside).
            "files": task.get("files") if isinstance(task.get("files"), dict) else {},
            "check": task.get("check") if isinstance(task.get("check"), dict) else {},
        }
        # SQL courses: the database to load before the student's query runs (seeds
        # aren't secret). Absent for non-SQL courses, so their client JSON is unchanged.
        seed = resolve_seed(slug, meta, task)
        if seed:
            entry["seed"] = seed
        client.append(entry)

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
