# -*- coding: utf-8 -*-
"""Learn routes: subject → main topics → subtopics browser.

Three endpoints under ``/learn/*``:

  - ``GET /learn``                       — subject grid landing page.
  - ``GET /learn/{subject}``             — accordion of main topics + subtopics.
  - ``GET /learn/{subject}/{subtopic}``  — placeholder ("learning program coming
    soon") for a chosen subtopic. The real learning UI slots in here later.

The two-level topic tree comes from ``web.syllabus_topics.load_topics`` reading
``syllabi/topics/<subject_key>.yaml`` — one file per subject, written by the
``python -m web.syllabus_topics`` one-shot extractor.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from eXam import open_mode

from ..syllabus_topics import load_topics
from ..template_ctx import template_ctx

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

router = APIRouter(prefix="/learn", tags=["learn"])

# Subtopic numbers may carry a Core/Extended prefix (Math: ``C1.1``, ``E1.1``)
# — mirror the normalizer regex in web/syllabus_topics.py.
_SUBTOPIC_PATH_RE = re.compile(r"^[A-Z]*\d+\.\d+$")


def _display_name(subject_key: str) -> str:
    from eXercise.config import PAGE_HEADER_BY_EXAM
    return PAGE_HEADER_BY_EXAM.get(subject_key) or subject_key.replace("_", " ").title()


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    subjects = [
        {**s, "available": load_topics(s["slug"]) is not None}
        for s in open_mode.subject_grid()
    ]
    return TEMPLATES.TemplateResponse(
        request,
        "learn/landing.html",
        template_ctx(request, subjects=subjects),
    )


@router.get("/{subject}", response_class=HTMLResponse)
async def topics_page(request: Request, subject: str):
    if subject not in {s["slug"] for s in open_mode.subject_grid()}:
        raise HTTPException(status_code=404, detail="Unknown subject")
    data = load_topics(subject)
    topics = (data or {}).get("topics") or []
    return TEMPLATES.TemplateResponse(
        request,
        "learn/topics.html",
        template_ctx(
            request,
            subject=subject,
            subject_display=_display_name(subject),
            topics=topics,
        ),
    )


@router.get("/{subject}/{subtopic}", response_class=HTMLResponse)
async def subtopic_page(request: Request, subject: str, subtopic: str):
    if not _SUBTOPIC_PATH_RE.match(subtopic):
        raise HTTPException(status_code=404, detail="Bad subtopic number")
    if subject not in {s["slug"] for s in open_mode.subject_grid()}:
        raise HTTPException(status_code=404, detail="Unknown subject")
    data = load_topics(subject)
    topics = (data or {}).get("topics") or []
    found_title: str | None = None
    parent_title: str | None = None
    for t in topics:
        for s in t.get("subtopics") or []:
            if s.get("number") == subtopic:
                found_title = s.get("title")
                parent_title = t.get("title")
                break
        if found_title is not None:
            break
    if found_title is None:
        raise HTTPException(status_code=404, detail="Unknown subtopic")
    return TEMPLATES.TemplateResponse(
        request,
        "learn/coming_soon.html",
        template_ctx(
            request,
            subject=subject,
            subject_display=_display_name(subject),
            subtopic_number=subtopic,
            subtopic_title=found_title,
            parent_title=parent_title,
        ),
    )
