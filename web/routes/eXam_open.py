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
    seen = open_mode.session_seen_qids(sid, subject)
    try:
        meta = open_mode.pick_random_question(subject, exclude=seen)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    open_mode.record_view(sid, meta["question_id"], subject)
    stats = open_mode.session_stats(sid, subject=subject)
    response.headers["Cache-Control"] = "no-store"
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


_REVIEW_FILTERS = {"viewed", "attempted", "correct"}


@router.get("/review/{filter_}", response_class=HTMLResponse)
async def review_redirect(filter_: str, subject: str | None = None):
    if filter_ not in _REVIEW_FILTERS:
        raise HTTPException(status_code=404, detail="Unknown filter")
    target = f"/eXam/practice/review/{filter_}/0"
    if subject:
        target += f"?subject={subject}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/review/{filter_}/{index}", response_class=HTMLResponse)
async def review(
    request: Request,
    response: Response,
    filter_: str,
    index: int,
    subject: str | None = None,
):
    if filter_ not in _REVIEW_FILTERS:
        raise HTTPException(status_code=404, detail="Unknown filter")
    sid = open_mode.ensure_session(request, response)

    subject_slugs = {s["slug"] for s in open_mode.subject_grid()}
    if subject is not None and subject not in subject_slugs:
        raise HTTPException(status_code=404, detail="Unknown subject")

    qids = open_mode.session_filtered_qids(sid, filter_, subject=subject)
    stats = open_mode.session_stats(sid, subject=subject)
    total = len(qids)
    response.headers["Cache-Control"] = "no-store"

    if total == 0:
        return TEMPLATES.TemplateResponse(
            "eXam/practice_take.html",
            template_ctx(
                request,
                review_mode=True,
                review_filter=filter_,
                review_subject=subject,
                review_empty=True,
                meta=None,
                stats=stats,
                subject="",
                subject_display="",
            ),
            headers=dict(response.headers),
        )

    if index < 0 or index >= total:
        raise HTTPException(status_code=404, detail="Index out of range")

    qid, qid_subject = qids[index]
    meta = open_mode.question_metadata(qid)
    if not meta:
        raise HTTPException(status_code=404, detail="Question metadata missing")

    past = open_mode.last_attempt(sid, qid)
    subject_display = next(
        (s["display"] for s in open_mode.subject_grid() if s["slug"] == qid_subject),
        qid_subject,
    )

    return TEMPLATES.TemplateResponse(
        "eXam/practice_take.html",
        template_ctx(
            request,
            review_mode=True,
            review_filter=filter_,
            review_subject=subject,
            review_index=index,
            review_total=total,
            review_empty=False,
            review_readonly=(filter_ == "correct"),
            review_past=past,
            subject=qid_subject,
            subject_display=subject_display,
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
    from eXam.render_helper import render_helper_markdown
    cached = read_cached(body.question_id, body.kind)
    if cached is not None:
        return {"ok": True, "content": render_helper_markdown(cached), "cache_hit": True}
    subject = body.question_id.split("::", 1)[0]
    try:
        content = pregenerate_for_question(
            {"question_id": body.question_id}, subject, body.kind,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Helper generation failed: {e}")
    return {"ok": True, "content": render_helper_markdown(content), "cache_hit": False}
