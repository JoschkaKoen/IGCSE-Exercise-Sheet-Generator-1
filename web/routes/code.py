# -*- coding: utf-8 -*-
"""Code routes: the in-browser Python learning playground.

  - ``GET /code``               — course / lesson index (landing).
  - ``GET /code/{slug}/{nn}``   — one lesson: prose + editor + console + tasks.

Lessons are authored markdown + YAML under ``content/code/<slug>/`` (loaded by
``web.code_content``). Student Python runs **entirely client-side** via Pyodide
in a Web Worker — nothing is executed on the server. To enable that worker's
``SharedArrayBuffer`` (needed for the Stop button's interrupt buffer and for
blocking ``input()``), the ``/code`` HTML responses carry cross-origin isolation
headers (COOP + COEP). These are scoped to ``/code`` only — the rest of the
site is unaffected.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from eXam.render_helper import render_helper_markdown

from .. import code_content
from ..i18n import detect_language
from ..template_ctx import template_ctx

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
# Lesson prose and task prompts are markdown; LaTeX survives as $…$ for KaTeX.
TEMPLATES.env.filters["render_md"] = render_helper_markdown

# Cross-origin isolation unlocks SharedArrayBuffer (Pyodide worker: interrupt
# buffer for Stop + Atomics-backed blocking input()). Scoped to /code responses
# only — never global. Everything the page loads is same-origin (the site
# self-hosts all assets), so COEP require-corp has no cross-origin subresource
# to block. no-cache mirrors the other content routes.
_CODE_HTML_HEADERS = {
    "Cache-Control": "no-cache, must-revalidate",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
}

router = APIRouter(prefix="/code", tags=["code"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    lang = detect_language(request)
    courses = code_content.list_courses(lang)
    return TEMPLATES.TemplateResponse(
        request,
        "code/landing.html",
        template_ctx(request, courses=courses),
        headers=_CODE_HTML_HEADERS,
    )


@router.get("/{slug}/{nn}", response_class=HTMLResponse)
async def lesson(request: Request, slug: str, nn: str):
    lang = detect_language(request)
    data = code_content.load_lesson(slug, nn, lang)
    if data is None:
        raise HTTPException(status_code=404, detail="Lesson not found")
    return TEMPLATES.TemplateResponse(
        request,
        "code/lesson.html",
        template_ctx(request, lesson=data),
        headers=_CODE_HTML_HEADERS,
    )
