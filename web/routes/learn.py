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

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from eXam import open_mode

from .. import extracted_questions
from ..handouts_collect import (
    HANDOUTS_ROOT,
    descriptive_pdf_name,
    load_glossary,
    load_handout_md,
    load_meta,
    meta_path,
    padded_topic,
    pdf_path,
    vocab_pdf_path,
)
from ..syllabus_content import load_content
from ..syllabus_topics import load_topics
from ..template_ctx import template_ctx
from ..templating import TEMPLATES, render_md

# Browser may cache but must revalidate — lets the landing-page prefetch warm
# the HTTP cache while keeping content fresh on the real click. Mirrors
# web/routes/site.py's _HTML_NO_CACHE.
_HTML_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}

router = APIRouter(prefix="/learn", tags=["learn"])

# Handout review (dev/review aid): compare the frozen English original against
# the live simplified + Chinese-glossed handout, side by side, in the browser.
BACKUP_ROOT = HANDOUTS_ROOT.parent / "handouts_en_backup"
REVIEW_SUBJECTS = ("a_level_physics", "a_level_computer_science")


def _read_backup_md(subject: str, topic: str) -> str | None:
    p = BACKUP_ROOT / subject / f"{padded_topic(topic)}.md"
    try:
        return p.read_text(encoding="utf-8") if p.is_file() else None
    except OSError:
        return None


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
    return render_md(_strip_h1(md)) if md else None


def warm_caches() -> None:
    """Best-effort: render every subtopic + handout once so the content/render
    caches are warm before the first visitor (the single worker would otherwise
    block on the cold render). Called in a background thread at startup."""
    try:
        subjects = [s["slug"] for s in open_mode.subject_grid()]
    except Exception:
        return
    for subject in subjects:
        try:
            data = load_topics(subject)
            for topic in (data or {}).get("topics") or []:
                subs = topic.get("subtopics") or []
                if subs:
                    for sub in subs:
                        _render_subtopic(subject, sub["number"])
                else:
                    _render_subtopic(subject, str(topic.get("number") or ""))
                num = str(topic.get("number") or "")
                if num and (md := load_handout_md(subject, num)):
                    render_md(md)
        except Exception:
            continue


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
        headers=_HTML_NO_CACHE,
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


@router.get("/handout-review", response_class=HTMLResponse)
async def handout_review_index(request: Request):
    groups = []
    for subj in REVIEW_SUBJECTS:
        data = load_topics(subj) or {}
        rows = []
        for topic in data.get("topics") or []:
            num = str(topic.get("number") or "")
            if not num or load_handout_md(subj, num) is None:
                continue
            meta = load_meta(meta_path(subj, num))
            if meta.get("glossed_at"):
                status = "glossed"
            elif meta.get("simplified_at"):
                status = "simplified"
            else:
                status = "original"
            rows.append({"topic": num, "title": topic.get("title") or "", "status": status})
        groups.append({"subject": subj, "display": _display_name(subj), "rows": rows})
    return TEMPLATES.TemplateResponse(
        request,
        "learn/handout_review_index.html",
        template_ctx(request, groups=groups),
    )


def _version_label(key: str) -> str:
    return {"original": "Original (English)", "current": "Current"}.get(key, key)


def _load_version_md(subject: str, topic: str, key: str) -> str | None:
    """Resolve a version key to its markdown: 'original' (backup), 'current'
    (live NN.md), or 'vN' (candidate NN.vN.md)."""
    if key == "original":
        return _read_backup_md(subject, topic)
    if key == "current":
        return load_handout_md(subject, topic)
    if re.fullmatch(r"v\d+", key or ""):
        p = HANDOUTS_ROOT / subject / f"{padded_topic(topic)}.{key}.md"
        try:
            return p.read_text(encoding="utf-8") if p.is_file() else None
        except OSError:
            return None
    return None


def _available_versions(subject: str, topic: str) -> list[str]:
    keys = ["original", "current"]
    pt = padded_topic(topic)
    for p in sorted((HANDOUTS_ROOT / subject).glob(f"{pt}.v*.md")):
        suffix = p.name[len(pt) + 1 : -3]  # "01.v3.md" → "v3"
        if re.fullmatch(r"v\d+", suffix):
            keys.append(suffix)
    return keys


@router.get("/handout-review/{subject}/{topic}", response_class=HTMLResponse)
async def handout_review(
    request: Request,
    subject: str,
    topic: str,
    left: str = "original",
    right: str = "current",
):
    if subject not in REVIEW_SUBJECTS:
        raise HTTPException(status_code=404, detail="Unknown subject")
    if load_handout_md(subject, topic) is None:
        raise HTTPException(status_code=404, detail="No handout for this topic")
    left_md = _load_version_md(subject, topic, left)
    right_md = _load_version_md(subject, topic, right)
    data = load_topics(subject) or {}
    title = next(
        (t.get("title") for t in data.get("topics") or [] if str(t.get("number")) == str(topic)),
        "",
    )
    return TEMPLATES.TemplateResponse(
        request,
        "learn/handout_review.html",
        template_ctx(
            request,
            subject=subject,
            subject_display=_display_name(subject),
            topic=str(topic),
            title=title or "",
            left_key=left,
            right_key=right,
            left_label=_version_label(left),
            right_label=_version_label(right),
            left_html=(render_md(left_md) if left_md else None),
            right_html=(render_md(right_md) if right_md else None),
            versions=_available_versions(subject, topic),
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
        topic_num = str(topic.get("number") or "")
        topic_title = str(topic.get("title") or "")
        md = load_handout_md(subject, topic_num) if topic_num else None
        topic["handout_html"] = render_md(md) if md else None
        # Vocab table renders wherever a glossary TSV exists (broader than the PDFs);
        # the download buttons below are gated separately on the compiled .pdf files.
        topic["vocab_rows"] = (load_glossary(subject, topic_num) or []) if topic_num else []
        if topic_num:
            pt = padded_topic(topic_num)
            # Served by the existing /handout-media static mount; ?v=<mtime> busts
            # the browser cache when a regenerated PDF is re-committed.
            pf = pdf_path(subject, topic_num)
            if pf.is_file():
                topic["handout_pdf_url"] = f"/handout-media/{subject}/pdf/{pt}.pdf?v={int(pf.stat().st_mtime)}"
                topic["handout_pdf_name"] = descriptive_pdf_name(
                    subject, topic_num, kind="handout", title=topic_title
                )
            vf = vocab_pdf_path(subject, topic_num)
            if vf.is_file():
                topic["vocab_pdf_url"] = f"/handout-media/{subject}/pdf/{pt}.vocab.pdf?v={int(vf.stat().st_mtime)}"
                topic["vocab_pdf_name"] = descriptive_pdf_name(
                    subject, topic_num, kind="vocab", title=topic_title
                )
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
