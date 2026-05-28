# -*- coding: utf-8 -*-
"""Learn routes: subject → main topics → subtopics browser.

Two endpoints under ``/learn/*``:

  - ``GET /learn``            — subject grid landing page.
  - ``GET /learn/{subject}``  — accordion of main topics; each subtopic
    expands inline to show its learning-objectives table (from
    ``syllabi/content/<subject>/<subtopic>.md``) or a "coming soon"
    placeholder when the markdown hasn't been extracted yet.

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

from .. import extracted_questions
from ..syllabus_content import load_content
from ..syllabus_topics import load_topics
from ..template_ctx import template_ctx

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
# Used by extracted_questions.html to render question/option text (LaTeX
# math survives as $…$ for KaTeX auto-render to pick up client-side).
TEMPLATES.env.filters["render_md"] = render_helper_markdown

router = APIRouter(prefix="/learn", tags=["learn"])


def _display_name(subject_key: str) -> str:
    from eXercise.config import PAGE_HEADER_BY_EXAM
    return PAGE_HEADER_BY_EXAM.get(subject_key) or subject_key.replace("_", " ").title()


def _strip_h1(md: str) -> str:
    # The .md is self-describing on disk (starts with "# N.M Title"), but
    # each subtopic's summary already shows that header inline — strip the
    # leading H1 so the rendered body doesn't double-title.
    return re.sub(r"^# [^\n]*\n+", "", md, count=1)


def _render_subtopic(subject: str, number: str) -> str | None:
    md = load_content(subject, number)
    return render_helper_markdown(_strip_h1(md)) if md else None


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


@router.get("/extracted", response_class=HTMLResponse)
async def extracted_landing(request: Request):
    subjects = extracted_questions.list_subjects()
    return TEMPLATES.TemplateResponse(
        request,
        "learn/extracted_landing.html",
        template_ctx(request, subjects=subjects),
    )


@router.get("/extracted/{subject}", response_class=HTMLResponse)
async def extracted_papers(request: Request, subject: str):
    valid = {s["slug"] for s in extracted_questions.list_subjects()}
    if subject not in valid:
        raise HTTPException(status_code=404, detail="Unknown subject")
    papers = extracted_questions.list_papers(subject)
    return TEMPLATES.TemplateResponse(
        request,
        "learn/extracted_papers.html",
        template_ctx(
            request,
            subject=subject,
            subject_display=_display_name(subject),
            papers=papers,
        ),
    )


@router.get("/extracted/{subject}/{paper_stem:path}", response_class=HTMLResponse)
async def extracted_paper(request: Request, subject: str, paper_stem: str):
    valid = {s["slug"] for s in extracted_questions.list_subjects()}
    if subject not in valid:
        raise HTTPException(status_code=404, detail="Unknown subject")
    data = extracted_questions.load_paper(subject, paper_stem)
    if data is None:
        raise HTTPException(status_code=404, detail="Unknown paper")
    questions = data.get("questions") or []
    matches = extracted_questions.load_subtopic_matches(subject, paper_stem) or {}
    extracted_questions.attach_matches(questions, matches)
    return TEMPLATES.TemplateResponse(
        request,
        "learn/extracted_questions.html",
        template_ctx(
            request,
            subject=subject,
            subject_display=_display_name(subject),
            paper_stem=paper_stem,
            questions=questions,
        ),
    )


@router.get("/{subject}", response_class=HTMLResponse)
async def topics_page(request: Request, subject: str):
    if subject not in {s["slug"] for s in open_mode.subject_grid()}:
        raise HTTPException(status_code=404, detail="Unknown subject")
    data = load_topics(subject)
    topics = (data or {}).get("topics") or []
    for topic in topics:
        subs = topic.get("subtopics") or []
        if subs:
            for sub in subs:
                sub["content_html"] = _render_subtopic(subject, sub["number"])
        else:
            topic["content_html"] = _render_subtopic(subject, str(topic.get("number") or ""))
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
