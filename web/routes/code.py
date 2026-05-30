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

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from eXam import open_mode

from .. import code_content, code_progress
from ..i18n import detect_language
from ..template_ctx import template_ctx
from ..templating import TEMPLATES
from ..user_auth import current_user_id

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


# ── Progress sync ───────────────────────────────────────────────────────────
# Server-backed mirror of the browser's localStorage (code.steps.v1 /
# code.progress.v1). Keyed on the anonymous open-mode session, attributed to a
# user_id once logged in (cross-device). The /code HTML pages are public, so
# these JSON routes are too — they only ever read/write the caller's own row.
# slug/nn are interpolated into a path, so they're regex-bounded before any
# filesystem touch; a missing lesson 404s before a session is minted.

_SLUG_RE = r"^[A-Za-z0-9_-]+$"
_NN_RE = r"^[A-Za-z0-9]+$"


def _lesson_exists(slug: str, nn: str) -> bool:
    """Cheap existence check — avoids a full load_lesson() parse on the hot path."""
    return (code_content.course_dir(slug) / f"{nn}.meta.yaml").is_file()


class _ProgressTask(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    done: bool = False
    attempts: int = Field(default=0, ge=0, le=1_000_000)


class _ProgressBody(BaseModel):
    slug: str = Field(pattern=_SLUG_RE, max_length=64)
    nn: str = Field(pattern=_NN_RE, max_length=16)
    revealed: int | None = Field(default=None, ge=0, le=1000)
    task: _ProgressTask | None = None


@router.get("/progress", response_class=JSONResponse)
async def get_progress(
    request: Request,
    response: Response,
    slug: str = Query(pattern=_SLUG_RE, max_length=64),
    nn: str = Query(pattern=_NN_RE, max_length=16),
):
    if not _lesson_exists(slug, nn):
        raise HTTPException(status_code=404, detail="Lesson not found")
    sid = open_mode.ensure_session(request, response)
    uid = current_user_id(request)
    data = code_progress.load_progress(sid, uid, slug, nn)
    response.headers["Cache-Control"] = "no-store"  # per-user data under a shared URL
    return JSONResponse(data, headers=dict(response.headers))


@router.post("/progress", response_class=JSONResponse)
async def post_progress(body: _ProgressBody, request: Request):
    if not _lesson_exists(body.slug, body.nn):
        raise HTTPException(status_code=404, detail="Lesson not found")
    # Don't mint a session on a write — the page's GET already did. No cookie
    # yet (e.g. cookies blocked) → no-op; the client keeps its localStorage.
    sid = open_mode.current_session_id(request)
    if not sid:
        return {"ok": False}
    uid = current_user_id(request)
    code_progress.save_progress(
        sid, uid, body.slug, body.nn,
        revealed=body.revealed,
        task=(body.task.model_dump() if body.task else None),
    )
    return {"ok": True}
