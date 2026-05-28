"""Open-mode practice: random picker + anonymous session helpers + stats.

Public, no login. A visitor picks a subject; we serve a random question from a
2025 paper for that subject; submit goes through ``eXam/marker.py``; helpers
go through ``eXam/pregenerate.py`` (file cache, lazy generation).
"""

from __future__ import annotations

import datetime as _dt
import json
import random
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import Final, Iterable

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
    """True iff *subject* has at least one *year* paper already indexed into the
    bank. Drives the landing page's enabled/disabled card state — only warmed
    subjects are clickable. Lazy-indexing on click would take ~30s and block
    the FastAPI event loop, so pre-warming is an offline admin step
    (``python -m eXam.warm_bank --subject <slug>``)."""
    return any(
        (bank_dir_for(subject, p) / "exam_questions.yaml").exists()
        for p in list_practice_papers(subject, year)
    )


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


@lru_cache(maxsize=64)
def _paper_candidates(paper_path: Path, subject: str) -> tuple[int, ...]:
    """Gradable qnums for *paper_path* whose snippet PDF exists on disk.
    Cached: papers don't change at runtime (warming is an offline admin step)."""
    bank = bank_dir_for(subject, paper_path)
    yaml_path = bank / "exam_questions.yaml"
    if not yaml_path.exists():
        return ()
    data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    out: list[int] = []
    for q in _gradable_top_level(data.get("questions") or []):
        qnum = int(q["number"])
        if (bank / str(qnum) / "question.pdf").exists():
            out.append(qnum)
    return tuple(out)


def pick_random_question(
    subject: str,
    year: int = 2025,
    *,
    rng: random.Random | None = None,
    exclude: Iterable[str] = (),
) -> dict:
    """Pick a random QP for *subject*+*year*, ensure it's bank-indexed, pick a
    random gradable question. Returns the dict shape ``question_metadata`` uses,
    plus ``paper_path`` and ``ms_path``.

    ``exclude`` is a set of question_ids (``subject::paper_stem::qnum``) to
    avoid — typically what the session has already been shown. If every paper's
    candidates are excluded, falls back to allowing repeats (preferable to a 503).
    """
    rng = rng or random.Random()
    exclude_set = set(exclude)
    papers = list(list_practice_papers(subject, year))
    if not papers:
        raise RuntimeError(f"No {year} papers for subject {subject!r}")
    # Prefer already-indexed papers (instant); fall back to lazy-indexing if none.
    indexed = [p for p in papers if (bank_dir_for(subject, p) / "exam_questions.yaml").exists()]
    if not indexed:
        paper_path = rng.choice(papers)
        ensure_paper_indexed(paper_path, pair_mark_scheme(paper_path), subject)
        indexed = [paper_path]

    def _qid(paper: Path, qnum: int) -> str:
        return f"{subject}::{paper.stem}::{qnum}"

    # Shuffle papers so the per-paper distribution stays uniform; iterate until
    # we find one with at least one un-excluded candidate.
    order = list(indexed)
    rng.shuffle(order)
    paper_path: Path | None = None
    qnum: int | None = None
    for cand_paper in order:
        cands = _paper_candidates(cand_paper, subject)
        unseen = [n for n in cands if _qid(cand_paper, n) not in exclude_set]
        if unseen:
            paper_path = cand_paper
            qnum = rng.choice(unseen)
            break

    if paper_path is None:
        # Pool exhausted for this session — fall back to original behaviour.
        paper_path = rng.choice(indexed)
        cands = _paper_candidates(paper_path, subject)
        if not cands:
            raise RuntimeError(f"Indexed paper has no rendered questions: {paper_path.name}")
        qnum = rng.choice(cands)

    # Safety net: snippet should exist (filter above), but ensure cache anyway.
    ensure_question_pdf(paper_path, qnum, subject=subject)
    qid = _qid(paper_path, qnum)
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


def _encode_submitted(submitted: dict[str, str] | str) -> str:
    """Serialise *submitted* for the ``open_attempts.submitted`` TEXT column.

    Per-leaf dicts are JSON-encoded; legacy strings pass through unchanged so
    a future class-mode migration can land without touching this code.
    """
    if isinstance(submitted, str):
        return submitted
    return json.dumps(submitted, ensure_ascii=False)


def _decode_submitted(text: str) -> dict[str, str] | str:
    """Inverse of :func:`_encode_submitted`. Falls back to the raw string for
    legacy rows written before per-leaf submission landed."""
    try:
        v = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(v, dict) and all(isinstance(k, str) for k in v):
        return {k: str(val) for k, val in v.items()}
    return text


def record_attempt(
    session_id: str,
    qid: str,
    subject: str,
    submitted: dict[str, str] | str,
    verdict: dict,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO open_attempts
                (session_id, question_id, subject, submitted, assigned_marks,
                 max_marks, reasoning, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id, qid, subject, _encode_submitted(submitted),
                float(verdict.get("assigned_marks", 0) or 0),
                float(verdict.get("max_marks", 0) or 0),
                verdict.get("reasoning") or "",
                _now(),
            ),
        )


def session_seen_qids(session_id: str, subject: str) -> set[str]:
    """Distinct question IDs the user has already been shown for *subject*."""
    if not session_id:
        return set()
    with connect() as conn:
        rows = conn.execute(
            "SELECT question_id FROM open_views WHERE session_id=? AND subject=?",
            (session_id, subject),
        ).fetchall()
    return {r["question_id"] for r in rows}


def record_view(session_id: str, qid: str, subject: str) -> None:
    """Record that *qid* has been shown to *session_id*. Idempotent — repeat
    shows of the same qid (after pool exhaustion) are silently ignored thanks
    to the ``UNIQUE(session_id, question_id)`` constraint."""
    if not session_id:
        return
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO open_views "
            "(session_id, question_id, subject, viewed_at) VALUES (?, ?, ?, ?)",
            (session_id, qid, subject, _now()),
        )


def session_stats(session_id: str, subject: str | None = None) -> dict:
    """Counters for the session UI. When *subject* is given, scopes all three
    counts to that subject (used on the take page so the header reflects "this
    subject in this session"). When None, counts across the session (used on
    the landing page for cross-subject totals).

    Returns ``{"viewed": int, "attempted": int, "correct": int}``.
    """
    with connect() as conn:
        if subject is None:
            viewed_row = conn.execute(
                "SELECT COUNT(*) AS n FROM open_views WHERE session_id=?",
                (session_id,),
            ).fetchone()
            attempts_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN assigned_marks >= max_marks AND max_marks > 0
                             THEN 1 ELSE 0 END) AS correct
                FROM open_attempts WHERE session_id=?
                """,
                (session_id,),
            ).fetchone()
        else:
            viewed_row = conn.execute(
                "SELECT COUNT(*) AS n FROM open_views WHERE session_id=? AND subject=?",
                (session_id, subject),
            ).fetchone()
            attempts_row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN assigned_marks >= max_marks AND max_marks > 0
                             THEN 1 ELSE 0 END) AS correct
                FROM open_attempts WHERE session_id=? AND subject=?
                """,
                (session_id, subject),
            ).fetchone()
    return {
        "viewed": int(viewed_row["n"] or 0),
        "attempted": int(attempts_row["total"] or 0),
        "correct": int(attempts_row["correct"] or 0),
    }


_REVIEW_FILTERS: Final[frozenset[str]] = frozenset({"viewed", "attempted", "correct"})


def session_filtered_qids(
    session_id: str,
    filter_: str,
    subject: str | None = None,
) -> list[tuple[str, str]]:
    """Ordered ``[(question_id, subject), …]`` for the review filter, oldest
    first. Filter values: ``"viewed"``, ``"attempted"``, ``"correct"``.

    - ``viewed``: every distinct question shown (``open_views``), by ``viewed_at``.
    - ``attempted``: every distinct question with ≥1 attempt, by first attempt.
    - ``correct``: every distinct question with ≥1 fully-correct attempt
      (``assigned_marks >= max_marks AND max_marks > 0``), by first correct attempt.

    Note: ``session_stats`` counts attempt *rows* for "attempted" / "correct",
    while this query groups by ``question_id`` (DISTINCT). When a user
    re-attempts the same question, the stat counter ticks up but this list
    stays the same length — by design. The duplicate-attempts case is rare
    because the picker excludes already-viewed qids until pool exhaustion.
    """
    if not session_id or filter_ not in _REVIEW_FILTERS:
        return []
    params: list = [session_id]
    where_subject = ""
    if subject is not None:
        where_subject = " AND subject=?"
        params.append(subject)
    if filter_ == "viewed":
        sql = (
            "SELECT question_id, subject FROM open_views "
            "WHERE session_id=?" + where_subject + " "
            "ORDER BY viewed_at ASC, id ASC"
        )
    elif filter_ == "attempted":
        sql = (
            "SELECT question_id, subject, MIN(submitted_at) AS first_at "
            "FROM open_attempts "
            "WHERE session_id=?" + where_subject + " "
            "GROUP BY question_id, subject "
            "ORDER BY first_at ASC"
        )
    else:  # "correct"
        sql = (
            "SELECT question_id, subject, MIN(submitted_at) AS first_at "
            "FROM open_attempts "
            "WHERE session_id=?" + where_subject + " "
            "AND assigned_marks >= max_marks AND max_marks > 0 "
            "GROUP BY question_id, subject "
            "ORDER BY first_at ASC"
        )
    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [(r["question_id"], r["subject"]) for r in rows]


def last_attempt(session_id: str, qid: str) -> dict | None:
    """Most recent ``open_attempts`` row for ``(session, qid)`` or None.

    Returns ``{submitted, assigned_marks, max_marks, reasoning, submitted_at}``
    — the shape the review-page past-attempt panel needs.
    """
    if not session_id:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT submitted, assigned_marks, max_marks, reasoning, submitted_at
            FROM open_attempts
            WHERE session_id=? AND question_id=?
            ORDER BY submitted_at DESC, id DESC
            LIMIT 1
            """,
            (session_id, qid),
        ).fetchone()
    if row is None:
        return None
    return {
        "submitted": _decode_submitted(row["submitted"] or ""),
        "assigned_marks": float(row["assigned_marks"] or 0),
        "max_marks": float(row["max_marks"] or 0),
        "reasoning": row["reasoning"] or "",
        "submitted_at": row["submitted_at"],
    }


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
                "level": "a_level" if slug.startswith("a_level_") else "igcse",
            }
        )
    return out
