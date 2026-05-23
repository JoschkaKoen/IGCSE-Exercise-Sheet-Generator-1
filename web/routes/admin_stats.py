# -*- coding: utf-8 -*-
"""Admin stats dashboard at GET /admin/stats.

Gated by the existing grade auth cookie (``esg_grade_auth``) — same gate as
the /grade page, no new auth model. Date-range filter via ``?days=7|30|90|all``.

This page is **cross-cutting** site stats: visitors, jobs, errors, plus a
single rollup of AI cost. It does NOT duplicate the per-test / per-student
detail that lives at /eXam/teacher/costs (linked at the top of the page).
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from ..analytics import store
from ..analytics.cost_overview import total_ai_cost_rmb
from ..grade_auth import is_grade_unlocked
from ..template_ctx import template_ctx

PACKAGE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))

router = APIRouter()


def _parse_days(raw: str) -> int | None:
    """Accept ``7|30|90|all`` (anything else → 30). ``all`` → None."""
    raw = (raw or "").strip().lower()
    if raw == "all":
        return None
    try:
        n = int(raw)
        if n in (1, 7, 30, 90, 365):
            return n
    except ValueError:
        pass
    return 30


@router.get("/admin/stats", response_class=HTMLResponse)
async def admin_stats(
    request: Request,
    days: str = Query("30"),
) -> HTMLResponse:
    """Stats overview. Redirects to /grade to authenticate when grade-locked."""
    if not is_grade_unlocked(request):
        # Bounce through /grade so the user enters the grade access code there
        # (no separate admin login form). After unlock, they navigate back.
        return RedirectResponse(url="/grade", status_code=303)

    days_val = _parse_days(days)
    totals = store.totals_for_range(days=days_val)
    pageviews_summary = store.pageviews_today_7d_30d()
    err_rate = store.error_rate(days=days_val)

    # Time-series: bucketed per UTC day. Two parallel series: pageviews vs jobs.
    series = store.events_by_day(
        days=days_val if days_val is not None else 90,  # cap "all" to 90d for chart sanity
        kinds=("pageview", "api_call", "grade_job_finished", "nl_job_finished"),
    )

    routes_top = store.top_routes(days=days_val, limit=15)
    jobs = store.recent_jobs(limit=50)
    errs = store.recent_errors(limit=50)
    cost_overview = total_ai_cost_rmb(days=days_val if days_val is not None else 30)
    sessions_n = store.session_count()

    # Pre-parse properties_json for the recent-jobs / errors tables. Done
    # server-side so the template renders plain dict access.
    for row in jobs:
        try:
            row["props"] = json.loads(row.get("properties_json") or "{}")
        except Exception:
            row["props"] = {}
    for row in errs:
        try:
            row["props"] = json.loads(row.get("properties_json") or "{}")
        except Exception:
            row["props"] = {}

    chart_payload = {
        "series": series,
        "days": days_val if days_val is not None else 90,
    }

    return TEMPLATES.TemplateResponse(
        request,
        "admin/stats.html",
        template_ctx(
            request,
            days=days_val,
            days_label="all" if days_val is None else str(days_val),
            totals=totals,
            pageviews_summary=pageviews_summary,
            error_rate=err_rate,
            routes_top=routes_top,
            recent_jobs=jobs,
            recent_errors=errs,
            cost_overview=cost_overview,
            sessions_n=sessions_n,
            chart_payload_json=json.dumps(chart_payload),
        ),
        headers={"Cache-Control": "no-store, no-cache"},
    )
