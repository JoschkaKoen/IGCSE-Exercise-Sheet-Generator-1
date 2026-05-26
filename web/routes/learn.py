# -*- coding: utf-8 -*-
"""Learn routes: subject → main topics → subtopics browser.

Three endpoints under ``/learn/*``:

  - ``GET /learn``                       — subject grid landing page.
  - ``GET /learn/{subject}``             — accordion of main topics + subtopics.
  - ``GET /learn/{subject}/{subtopic}``  — renders the per-subtopic
    learning-objectives table (from ``syllabi/content/<subject>/<subtopic>.md``)
    when extracted; otherwise a "learning program coming soon" placeholder.

The two-level topic tree comes from ``web.syllabus_topics.load_topics`` reading
``syllabi/topics/<subject_key>.yaml`` (written by ``python -m web.syllabus_topics``).
The per-subtopic markdown comes from ``web.syllabus_content.load_content``
reading ``syllabi/content/<subject>/<subtopic>.md`` (written by
``python -m web.syllabus_content``).
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from eXam import open_mode
from eXam.render_helper import render_helper_markdown

from ..syllabus_content import load_content
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
    content_md = load_content(subject, subtopic)
    content_html = render_helper_markdown(content_md) if content_md else None
    return TEMPLATES.TemplateResponse(
        request,
        "learn/subtopic.html",
        template_ctx(
            request,
            subject=subject,
            subject_display=_display_name(subject),
            subtopic_number=subtopic,
            subtopic_title=found_title,
            parent_title=parent_title,
            content_html=content_html,
        ),
    )
