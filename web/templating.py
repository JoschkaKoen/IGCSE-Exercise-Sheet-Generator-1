# -*- coding: utf-8 -*-
"""Shared Jinja2 templates environment.

A single ``Jinja2Templates`` for the whole app so filters/globals are registered
once. Previously each router built its own instance (identical directory) and
two of them (``learn``/``code``) re-registered the ``render_md`` filter. Routers
now ``from ..templating import TEMPLATES``.

Provides:
- ``render_md`` filter — cached markdown→HTML for the env-free render path.
- ``static_url`` global — ``/static/<path>?v=<mtime>`` cache-busting helper.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi.templating import Jinja2Templates

from eXam.render_helper import render_helper_markdown

PACKAGE_DIR = Path(__file__).resolve().parent  # web/
STATIC_DIR = PACKAGE_DIR / "static"
TEMPLATES_DIR = PACKAGE_DIR / "templates"

TEMPLATES = Jinja2Templates(directory=str(TEMPLATES_DIR))


@lru_cache(maxsize=2048)
def _render_md_cached(src: str) -> str:
    """Cached markdown→HTML for the env-free render path (subtopic content, code
    prose, the ``render_md`` filter).

    ``render_helper_markdown`` is a pure function of ``src`` ONLY when no ``env``
    is passed. The handout path passes ``env={"img_dims": ...}`` (output depends
    on figure files on disk) and must NOT route through this cache — see
    ``web/routes/learn.py``.
    """
    return render_helper_markdown(src)


def render_md(src: str) -> str:
    """Jinja ``render_md`` filter — cached, env-free (markup is emitted raw, as
    before; templates apply ``| safe`` where needed)."""
    return _render_md_cached(src) if src else ""


def static_url(path: str) -> str:
    """``/static/<path>?v=<mtime>`` so an edited asset busts the browser cache
    immediately (the /static mount is short-max-age, not immutable).

    Stat fresh each call — cheap, and required so dev ``--reload`` edits
    propagate; deliberately not memoised.
    """
    rel = str(path).lstrip("/")
    try:
        version = int((STATIC_DIR / rel).stat().st_mtime)
    except OSError:
        return f"/static/{rel}"
    return f"/static/{rel}?v={version}"


TEMPLATES.env.filters["render_md"] = render_md
TEMPLATES.env.globals["static_url"] = static_url
