# -*- coding: utf-8 -*-
"""Per-user dashboard page.

Single GET route at ``/dashboard``. Aggregates everything the logged-in user
can act on:

  - profile (username, role, member-since, last login) + the two settings forms
    (POSTs to ``/api/account/change-username`` and ``/api/account/change-password``)
  - AI cost summary (NL + xScore from the ``jobs`` table, eXam from ``ai_calls``)
  - persisted NL exercise-sheet jobs (``jobs WHERE kind='nl'``)
  - persisted grade jobs (``jobs WHERE kind='grade'``)
  - eXam activity: tests built (teacher-only), open-mode practice by subject,
    and a 12-week practice-activity series for the Chart.js bar chart.

Anonymous visitors are redirected to ``/`` — the chip there opens the
account-creation modal.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from eXam.db import connect
from .. import jobs_db
from ..template_ctx import template_ctx
from ..user_auth import current_user_id
from .account import get_full_by_id

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

router = APIRouter()


def _ai_cost_summary(user_id: int) -> dict[str, Any]:
    """Three-pipeline cost rollup. NL+grade from jobs.total_cost_rmb,
    eXam from ai_calls.cost_rmb filtered by pipeline='exam'."""
    nl = jobs_db.total_cost_for_user(user_id, kind="nl")
    grade = jobs_db.total_cost_for_user(user_id, kind="grade")
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost_rmb), 0.0) AS total "
                "FROM ai_calls WHERE user_id = ? AND pipeline = 'exam'",
                (user_id,),
            ).fetchone()
            exam = float(row["total"] or 0.0) if row else 0.0
    except Exception:
        exam = 0.0
    return {
        "nl_rmb": round(nl, 4),
        "grade_rmb": round(grade, 4),
        "exam_rmb": round(exam, 4),
        "total_rmb": round(nl + grade + exam, 4),
    }


def _tests_built(user_id: int) -> list[dict[str, Any]]:
    """Most-recent 5 tests this teacher built (empty for non-teachers / no rows)."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, title, subject, class_label, status, created_at, ready_at "
                "FROM tests WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT 5",
                (user_id,),
            ).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


def _tests_built_count(user_id: int) -> int:
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM tests WHERE user_id = ?", (user_id,),
            ).fetchone()
            return int(row["c"] or 0) if row else 0
    except Exception:
        return 0


def _open_practice_by_subject(user_id: int) -> list[dict[str, Any]]:
    """Per-subject attempt count + average score for the open-mode block."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT subject, COUNT(*) AS n, "
                "       AVG(CASE WHEN max_marks > 0 THEN assigned_marks * 1.0 / max_marks END) AS avg_frac "
                "FROM open_attempts WHERE user_id = ? "
                "GROUP BY subject ORDER BY n DESC",
                (user_id,),
            ).fetchall()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        avg = r["avg_frac"]
        out.append({
            "subject": r["subject"],
            "n": int(r["n"] or 0),
            "avg_pct": round(float(avg) * 100, 1) if avg is not None else None,
        })
    return out


def _weekly_practice(user_id: int, *, weeks: int = 12) -> list[dict[str, Any]]:
    """Questions submitted per ISO week, last ``weeks`` weeks. Bar chart input.

    SQLite's ``strftime('%Y-%W', ...)`` bucketing is deterministic and avoids
    pulling a pandas / dateutil dependency just for the chart.
    """
    days = weeks * 7
    try:
        with connect() as conn:
            rows = conn.execute(
                f"SELECT strftime('%Y-%W', submitted_at) AS week, COUNT(*) AS n "
                f"FROM open_attempts "
                f"WHERE user_id = ? AND submitted_at >= date('now', '-{days} days') "
                f"GROUP BY week ORDER BY week",
                (user_id,),
            ).fetchall()
    except Exception:
        return []
    return [{"week": r["week"], "n": int(r["n"] or 0)} for r in rows]


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    uid = current_user_id(request)
    if uid is None:
        return RedirectResponse(url="/", status_code=302)
    user = get_full_by_id(uid)
    if user is None:
        return RedirectResponse(url="/", status_code=302)

    nl_jobs = jobs_db.list_for_user(uid, kind="nl", limit=50)
    grade_jobs = jobs_db.list_for_user(uid, kind="grade", limit=50)
    costs = _ai_cost_summary(uid)
    tests_built = _tests_built(uid) if user["role"] == "teacher" else []
    tests_built_total = _tests_built_count(uid) if user["role"] == "teacher" else 0
    open_practice = _open_practice_by_subject(uid)
    weekly = _weekly_practice(uid)

    return TEMPLATES.TemplateResponse(
        request,
        "dashboard.html",
        template_ctx(
            request,
            profile=user,
            costs=costs,
            nl_jobs=nl_jobs,
            grade_jobs=grade_jobs,
            tests_built=tests_built,
            tests_built_total=tests_built_total,
            open_practice=open_practice,
            weekly_json=json.dumps(weekly),
        ),
    )
