# -*- coding: utf-8 -*-
"""eXam open-mode (public) routes: anonymous practice from random 2025 papers.

No login. Anonymous session cookie tracks per-session stats. Mounted at
``/eXam/practice/*`` so it does not collide with class-mode routes at
``/eXam/login``, ``/eXam/``, ``/eXam/test/<id>``.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from eXam import open_mode
from eXam.runtime import pdf_path_for

from ..template_ctx import template_ctx

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

router = APIRouter(prefix="/eXam/practice", tags=["eXam-open"])


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def landing(request: Request, response: Response):
    sid = open_mode.ensure_session(request, response)
    stats = open_mode.session_stats(sid)
    subjects = open_mode.subject_grid()
    return TEMPLATES.TemplateResponse(
        "eXam/practice_landing.html",
        template_ctx(request, subjects=subjects, stats=stats),
        headers=dict(response.headers),
    )


@router.get("/{subject}", response_class=HTMLResponse)
async def take(request: Request, response: Response, subject: str):
    if subject not in {s["slug"] for s in open_mode.subject_grid()}:
        raise HTTPException(status_code=404, detail="Unknown subject")
    sid = open_mode.ensure_session(request, response)
    try:
        meta = open_mode.pick_random_question(subject)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    stats = open_mode.session_stats(sid)
    return TEMPLATES.TemplateResponse(
        "eXam/practice_take.html",
        template_ctx(
            request,
            subject=subject,
            subject_display=next(
                (s["display"] for s in open_mode.subject_grid() if s["slug"] == subject),
                subject,
            ),
            meta=meta,
            stats=stats,
        ),
        headers=dict(response.headers),
    )


@router.get("/pdf/{question_id:path}")
async def serve_pdf(request: Request, question_id: str):
    path = pdf_path_for(question_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Snippet not found")
    return FileResponse(
        str(path),
        media_type="application/pdf",
        headers={"Cache-Control": "public, max-age=3600"},
    )


class SubmitBody(BaseModel):
    subject: str
    question_id: str
    submitted: str = Field(..., max_length=10000)


@router.post("/submit", response_class=JSONResponse)
@router.post("/api/submit", response_class=JSONResponse)  # legacy alias
async def submit(body: SubmitBody, request: Request, response: Response):
    sid = open_mode.ensure_session(request, response)
    from eXam.marker import mark as marker_mark

    try:
        verdict = marker_mark(0, body.question_id, body.submitted)  # student_id unused in open mode
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Marker unavailable: {e}")
    open_mode.record_attempt(sid, body.question_id, body.subject, body.submitted, verdict)
    stats = open_mode.session_stats(sid)
    return {
        "ok": True,
        "assigned_marks": verdict["assigned_marks"],
        "max_marks": verdict["max_marks"],
        "reasoning": verdict.get("reasoning") or "",
        "stats": stats,
    }


class HelperBody(BaseModel):
    question_id: str
    kind: str


@router.post("/helper", response_class=JSONResponse)
@router.post("/api/helper", response_class=JSONResponse)  # legacy alias
async def helper(body: HelperBody, request: Request):
    if body.kind not in {"hint", "solution", "example", "kb"}:
        raise HTTPException(status_code=400, detail="Bad helper kind")
    from eXam.pregenerate import pregenerate_for_question, read_cached
    cached = read_cached(body.question_id, body.kind)
    if cached is not None:
        return {"ok": True, "content": cached, "cache_hit": True}
    subject = body.question_id.split("::", 1)[0]
    try:
        content = pregenerate_for_question(
            {"question_id": body.question_id}, subject, body.kind,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Helper generation failed: {e}")
    return {"ok": True, "content": content, "cache_hit": False}
