# -*- coding: utf-8 -*-
"""eXam open-mode (public) routes: anonymous practice from random recent papers.

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
from pydantic import BaseModel, Field, field_validator

from eXam import open_mode
from eXam.runtime import pdf_path_for

from ..handouts_collect import topic_qids
from ..syllabus_topics import load_topics
from ..template_ctx import template_ctx

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

router = APIRouter(prefix="/eXam/practice", tags=["eXam-open"])

# Browser may cache but must revalidate — lets the landing-page prefetch warm
# the HTTP cache while keeping content fresh on the real click.
_HTML_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


def _is_prefetch(request: Request) -> bool:
    """True when the browser is speculatively fetching, not really navigating.

    Chromium sets ``Sec-Purpose: prefetch`` on speculation-rules and
    ``<link rel="prefetch">`` requests; older engines use ``Purpose: prefetch``.
    A speculative hit must not mint an anonymous session or set a cookie.
    """
    return (
        request.headers.get("sec-purpose", "").lower().startswith("prefetch")
        or request.headers.get("purpose", "").lower() == "prefetch"
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def landing(request: Request, response: Response):
    subjects = open_mode.subject_grid()
    if _is_prefetch(request):
        # Warm the cache without minting a session or setting a cookie.
        stats = {"viewed": 0, "attempted": 0, "correct": 0}
        return TEMPLATES.TemplateResponse(
            request,
            "eXam/practice_landing.html",
            template_ctx(request, subjects=subjects, stats=stats),
            headers=_HTML_NO_CACHE,
        )
    sid = open_mode.ensure_session(request, response)
    stats = open_mode.session_stats(sid)
    return TEMPLATES.TemplateResponse(
        request,
        "eXam/practice_landing.html",
        template_ctx(request, subjects=subjects, stats=stats),
        headers={**dict(response.headers), **_HTML_NO_CACHE},
    )


def _build_topic_picker(subject: str, sid: str) -> list[dict]:
    """Topic chips for *subject*: ``{number, title, available, attempted, correct}``.

    Drops topics with no servable question (so an empty topic never shows / is
    never linkable). ``available`` = matched ∩ rendered-snippet; the activity
    counts intersect the topic's qids with the session's attempted/correct sets
    (fetched once each — not per topic). Empty when the subject has no topic YAML.
    """
    topics = (load_topics(subject) or {}).get("topics") or []
    if not topics:
        return []
    qids_by_topic = topic_qids(subject)
    servable = open_mode.subject_candidate_qids(subject)
    correct = {q for q, _ in open_mode.session_filtered_qids(sid, "correct", subject=subject)}
    attempted = {q for q, _ in open_mode.session_filtered_qids(sid, "attempted", subject=subject)}
    out: list[dict] = []
    for t in topics:
        num = str(t.get("number") or "").strip()
        if not num:
            continue
        qids = qids_by_topic.get(num, frozenset()) & servable
        if not qids:
            continue
        out.append({
            "number": num,
            "title": t.get("title") or num,
            "available": len(qids),
            "attempted": len(qids & attempted),
            "correct": len(qids & correct),
        })
    return out


@router.get("/{subject}", response_class=HTMLResponse)
async def take(request: Request, response: Response, subject: str, topic: str | None = None):
    grid = open_mode.subject_grid()
    if subject not in {s["slug"] for s in grid}:
        raise HTTPException(status_code=404, detail="Unknown subject")
    sid = open_mode.ensure_session(request, response)

    topics = _build_topic_picker(subject, sid)
    allow = None
    active_topic = None
    if topic is not None:
        if topic not in {t["number"] for t in topics}:
            raise HTTPException(status_code=404, detail="Unknown topic")
        allow = topic_qids(subject).get(topic) or set()
        active_topic = topic

    seen = open_mode.session_seen_qids(sid, subject)
    try:
        meta = open_mode.pick_random_question(subject, exclude=seen, allow=allow)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    open_mode.record_view(sid, meta["question_id"], subject)
    stats = open_mode.session_stats(sid, subject=subject)
    response.headers["Cache-Control"] = "no-store"
    return TEMPLATES.TemplateResponse(
        request,
        "eXam/practice_take.html",
        template_ctx(
            request,
            subject=subject,
            subject_display=next(
                (s["display"] for s in grid if s["slug"] == subject),
                subject,
            ),
            meta=meta,
            stats=stats,
            topics=topics,
            active_topic=active_topic,
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
            request,
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
        request,
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


@router.get("/regions/{question_id:path}")
async def serve_regions(request: Request, question_id: str):
    from eXam.regions import ensure_question_regions
    try:
        path = ensure_question_regions(question_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Snippet not found")
    except Exception as e:  # noqa: BLE001 — never let detector failure 500 the page
        return JSONResponse(
            {"detector_version": 0, "regions": [], "error": str(e)},
            status_code=200,
        )
    return FileResponse(
        str(path),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=300"},
    )


class SubmitBody(BaseModel):
    subject: str
    question_id: str
    submitted: dict[str, str]

    @field_validator("submitted")
    @classmethod
    def _cap_total_length(cls, v: dict[str, str]) -> dict[str, str]:
        if sum(len(s) for s in v.values()) > 10_000:
            raise ValueError("submitted exceeds 10 000 characters total")
        if len(v) > 64:
            raise ValueError("too many submitted leaves")
        return v


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
