"""Open-mode practice: random picker + anonymous session helpers + stats.

Public, no login. A visitor picks a subject; we serve a random question from a
2025 paper for that subject; submit goes through ``eXam/marker.py``; helpers
go through ``eXam/pregenerate.py`` (file cache, lazy generation).
"""

from __future__ import annotations

import datetime as _dt
import random
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Final

import yaml
from fastapi import Request, Response

from eXam.bank import bank_dir_for, ensure_paper_indexed, ensure_question_pdf
from eXam.db import connect
from eXam.runtime import question_metadata
from eXercise.config import EXAM_ROOT_BY_KEY

COOKIE_NAME: Final[str] = "esg_eXam_open"
_COOKIE_TTL_S: Final[int] = 30 * 24 * 60 * 60  # 30 days


# ── Library scanning ────────────────────────────────────────────────────────

@lru_cache(maxsize=64)
def list_practice_papers(subject: str, year: int = 2025) -> tuple[Path, ...]:
    """Return QPs (not mark schemes) under ``exams/<subject>/`` for *year*.
    Tuple so lru_cache stays hashable.

    Handles two filename conventions:
    - Human-readable: ``0625 Physics June 2025 Question Paper 11.pdf``
    - Cambridge code: ``0478_s25_qp_11.pdf`` (``s``/``w``/``m`` = May/Oct/March series)
    """
    root = EXAM_ROOT_BY_KEY.get(subject)
    if root is None or not root.is_dir():
        return ()
    year_s = str(year)
    code_re = re.compile(rf"_[smw]{year_s[-2:]}_")  # _s25_ / _w25_ / _m25_
    out: list[Path] = []
    for p in sorted(root.glob("*.pdf")):
        name = p.name
        if year_s not in name and not code_re.search(name):
            continue
        # Must be a question paper (human-readable label or `_qp_` code).
        if "Question Paper" not in name and "_qp_" not in name:
            continue
        if any(tag in name for tag in ("Mark Scheme", "Examiner Report", "Grade Threshold")):
            continue
        out.append(p)
    return tuple(out)


def pair_mark_scheme(qp_path: Path) -> Path | None:
    """Find the matching mark scheme for *qp_path*. Supports human-readable
    (``Question Paper`` → ``Mark Scheme``) and Cambridge code (``_qp_`` →
    ``_ms_``) filename schemes."""
    name = qp_path.name
    if "Question Paper" in name:
        ms_name = name.replace("Question Paper", "Mark Scheme")
    elif "_qp_" in name:
        ms_name = name.replace("_qp_", "_ms_")
    else:
        return None
    ms = qp_path.with_name(ms_name)
    return ms if ms.exists() else None


def subject_has_papers(subject: str, year: int = 2025) -> bool:
    """True if *subject* has any *year* question papers on disk under ``exams/``.
    Drives the landing page's enabled/disabled card state — un-warmed subjects
    still show as available; their first click lazy-indexes a paper via
    ``pick_random_question`` (slower first request, no AI cost up front)."""
    return bool(list_practice_papers(subject, year))


def _gradable_top_level(qs: list[dict]) -> list[dict]:
    """Top-level questions only; skip non-integer numbers."""
    out: list[dict] = []
    for q in qs:
        if not isinstance(q, dict):
            continue
        try:
            int(q.get("number"))
        except (TypeError, ValueError):
            continue
        out.append(q)
    return out


def pick_random_question(subject: str, year: int = 2025, *, rng: random.Random | None = None) -> dict:
    """Pick a random QP for *subject*+*year*, ensure it's bank-indexed, pick a
    random gradable question. Returns the dict shape ``question_metadata`` uses,
    plus ``paper_path`` and ``ms_path``.
    """
    rng = rng or random.Random()
    papers = list(list_practice_papers(subject, year))
    if not papers:
        raise RuntimeError(f"No {year} papers for subject {subject!r}")
    # Prefer already-indexed papers (instant); fall back to lazy-indexing if none.
    indexed = [p for p in papers if (bank_dir_for(subject, p) / "exam_questions.yaml").exists()]
    if indexed:
        paper_path = rng.choice(indexed)
    else:
        paper_path = rng.choice(papers)
        ensure_paper_indexed(paper_path, pair_mark_scheme(paper_path), subject)
    bank = bank_dir_for(subject, paper_path)
    yaml_path = bank / "exam_questions.yaml"
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    candidates = _gradable_top_level(data.get("questions") or [])
    if not candidates:
        raise RuntimeError(f"Indexed paper has no gradable questions: {paper_path.name}")
    q = rng.choice(candidates)
    qnum = int(q["number"])
    # Make sure the per-question PDF snippet exists.
    ensure_question_pdf(paper_path, qnum, subject=subject)
    qid = f"{subject}::{paper_path.stem}::{qnum}"
    meta = question_metadata(qid) or {}
    meta["paper_path"] = str(paper_path)
    meta["ms_path"] = str(pair_mark_scheme(paper_path) or "") or None
    return meta


# ── Anonymous session cookie ────────────────────────────────────────────────

_UUID_RE = re.compile(r"^[0-9a-fA-F-]{32,40}$")


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def ensure_session(request: Request, response: Response) -> str:
    """Read or create the anonymous session cookie. Returns the session_id and
    sets the cookie on ``response`` (idempotent — only writes when missing)."""
    raw = (request.cookies.get(COOKIE_NAME) or "").strip()
    if raw and _UUID_RE.match(raw):
        with connect() as conn:
            row = conn.execute("SELECT id FROM open_sessions WHERE id=?", (raw,)).fetchone()
        if row is not None:
            with connect() as conn:
                conn.execute(
                    "UPDATE open_sessions SET last_seen_at=? WHERE id=?",
                    (_now(), raw),
                )
            return raw
    # New session.
    sid = uuid.uuid4().hex
    with connect() as conn:
        conn.execute(
            "INSERT INTO open_sessions (id, created_at, last_seen_at) VALUES (?, ?, ?)",
            (sid, _now(), _now()),
        )
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        key=COOKIE_NAME,
        value=sid,
        max_age=_COOKIE_TTL_S,
        httponly=True,
        samesite="lax",
        path="/",
        secure=(proto.lower() == "https"),
    )
    return sid


def current_session_id(request: Request) -> str | None:
    raw = (request.cookies.get(COOKIE_NAME) or "").strip()
    return raw if raw and _UUID_RE.match(raw) else None


def record_attempt(session_id: str, qid: str, subject: str, submitted: str, verdict: dict) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO open_attempts
                (session_id, question_id, subject, submitted, assigned_marks,
                 max_marks, reasoning, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, qid, subject, submitted,
                float(verdict.get("assigned_marks", 0) or 0),
                float(verdict.get("max_marks", 0) or 0),
                verdict.get("reasoning") or "",
                _now(),
            ),
        )


def session_stats(session_id: str) -> dict:
    """Live count of attempts + correct (assigned == max, max > 0)."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN assigned_marks >= max_marks AND max_marks > 0
                         THEN 1 ELSE 0 END) AS correct
            FROM open_attempts WHERE session_id=?
            """,
            (session_id,),
        ).fetchone()
    return {"total": int(row["total"] or 0), "correct": int(row["correct"] or 0)}


def subject_grid() -> list[dict]:
    """Cards for the landing page. One per subject in EXAM_ROOT_BY_KEY."""
    from eXercise.config import EXAM_ROOT_BY_KEY
    # Display names: prefer PAGE_HEADER_BY_EXAM if present, else humanise slug.
    try:
        from eXercise.config import PAGE_HEADER_BY_EXAM as _NAMES
    except ImportError:
        _NAMES = {}
    out: list[dict] = []
    for slug in EXAM_ROOT_BY_KEY:
        out.append(
            {
                "slug": slug,
                "display": _NAMES.get(slug) or slug.replace("_", " ").title(),
                "available": subject_has_papers(slug),
            }
        )
    return out
