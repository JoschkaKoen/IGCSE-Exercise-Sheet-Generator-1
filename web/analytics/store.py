# -*- coding: utf-8 -*-
"""SQLite-backed analytics store.

A single shared connection (guarded by a ``threading.Lock``) at
``ANALYTICS_DB_PATH`` (default ``/app/output/analytics/analytics.db``). WAL
journal mode lets the dashboard read while the middleware writes; both run
in the same FastAPI process, so a single connection is fine.

Storage scope is deliberately narrow: page views, sessions, job lifecycle,
auth attempts, exam submissions, errors. **AI cost data is NOT stored here**
— it lives in xScore's ``cost.json`` files and eXam's ``ai_calls`` table.
See :mod:`web.analytics.cost_overview` for the read-time union.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Repo root is …/eXercise/; this file is at …/eXercise/web/analytics/store.py
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB_PATH = _REPO_ROOT / "output" / "analytics" / "analytics.db"
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

_log = logging.getLogger(__name__)


def _db_path() -> Path:
    env = os.environ.get("ANALYTICS_DB_PATH")
    return Path(env) if env else _DEFAULT_DB_PATH


def init_db() -> None:
    """Create the DB + schema if missing. Safe to call multiple times."""
    global _conn
    with _lock:
        path = _db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(path),
            check_same_thread=False,
            isolation_level=None,  # autocommit; we do explicit BEGIN where needed
        )
        conn.row_factory = sqlite3.Row
        # WAL mode is set inside schema.sql too, but apply explicitly so the
        # first connection sees it even before the script runs.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        ddl = _SCHEMA_PATH.read_text(encoding="utf-8")
        conn.executescript(ddl)
        _conn = conn


def _get_conn() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    assert _conn is not None
    return _conn


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


# ---------------------------------------------------------------------------
# Writes — silent on failure so analytics can never break a request.
# ---------------------------------------------------------------------------

def insert_event(
    *,
    kind: str,
    ts: str | None = None,
    method: str | None = None,
    session_id: str | None = None,
    route: str | None = None,
    referrer: str | None = None,
    status: str | None = None,
    user_kind: str | None = None,
    properties_json: str | None = None,
    duration_ms: int | None = None,
) -> None:
    """Append one row to ``events``. Never raises."""
    try:
        conn = _get_conn()
        with _lock:
            conn.execute(
                """
                INSERT INTO events
                    (ts, kind, method, session_id, route, referrer, status,
                     user_kind, properties_json, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts or _now_iso(),
                    kind,
                    method,
                    session_id,
                    route,
                    referrer[:256] if referrer else None,
                    status,
                    user_kind,
                    properties_json,
                    duration_ms,
                ),
            )
    except Exception:
        _log.exception("analytics.insert_event failed (kind=%s)", kind)


def upsert_session(
    *,
    session_id: str,
    user_kind: str | None,
    ua_browser: str | None,
    ua_os: str | None,
    is_mobile: bool,
    ip_hash: str | None,
) -> None:
    """Create or touch a row in ``sessions``. Never raises."""
    try:
        conn = _get_conn()
        now = _now_iso()
        with _lock:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, first_seen, last_seen, user_kind, ua_browser, ua_os,
                     is_mobile, ip_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    last_seen  = excluded.last_seen,
                    user_kind  = COALESCE(excluded.user_kind,  sessions.user_kind),
                    ua_browser = COALESCE(excluded.ua_browser, sessions.ua_browser),
                    ua_os      = COALESCE(excluded.ua_os,      sessions.ua_os),
                    is_mobile  = COALESCE(excluded.is_mobile,  sessions.is_mobile),
                    ip_hash    = COALESCE(excluded.ip_hash,    sessions.ip_hash)
                """,
                (
                    session_id,
                    now,
                    now,
                    user_kind,
                    ua_browser,
                    ua_os,
                    1 if is_mobile else 0,
                    ip_hash,
                ),
            )
    except Exception:
        _log.exception("analytics.upsert_session failed (id=%s)", session_id)


# ---------------------------------------------------------------------------
# Reads — dashboard queries. Range arg ``days`` is None → "all time".
# ---------------------------------------------------------------------------

def _since_iso(days: int | None) -> str:
    """Return the ISO cutoff for ``last *days*`` (None → epoch)."""
    if days is None or days <= 0:
        return "1970-01-01T00:00:00+00:00"
    cutoff = _dt.datetime.now(_dt.UTC) - _dt.timedelta(days=days)
    return cutoff.isoformat()


def totals_for_range(*, days: int | None) -> dict[str, int]:
    """High-level overview counters for the date range."""
    conn = _get_conn()
    since = _since_iso(days)
    pageviews = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind IN ('pageview','api_call') AND ts >= ?",
        (since,),
    ).fetchone()[0]
    visitors = conn.execute(
        "SELECT COUNT(DISTINCT session_id) FROM events "
        "WHERE session_id IS NOT NULL AND ts >= ?",
        (since,),
    ).fetchone()[0]
    jobs = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind IN "
        "('grade_job_finished','nl_job_finished') AND ts >= ?",
        (since,),
    ).fetchone()[0]
    errors = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='error' AND ts >= ?",
        (since,),
    ).fetchone()[0]
    submissions = conn.execute(
        "SELECT COUNT(*) FROM events WHERE kind='exam_submission' AND ts >= ?",
        (since,),
    ).fetchone()[0]
    return {
        "pageviews": int(pageviews or 0),
        "visitors": int(visitors or 0),
        "jobs": int(jobs or 0),
        "errors": int(errors or 0),
        "submissions": int(submissions or 0),
    }


def pageviews_today_7d_30d() -> dict[str, int]:
    """Three discrete counts for the overview card."""
    return {
        "today": totals_for_range(days=1)["pageviews"],
        "last_7d": totals_for_range(days=7)["pageviews"],
        "last_30d": totals_for_range(days=30)["pageviews"],
    }


def error_rate(*, days: int | None) -> float:
    """COUNT(error) / COUNT(pageview|api_call). Zero requests → 0.0."""
    t = totals_for_range(days=days)
    denom = t["pageviews"]
    return (t["errors"] / denom) if denom else 0.0


def events_by_day(*, days: int | None, kinds: Iterable[str]) -> list[dict[str, Any]]:
    """Per-day counts for the given event kinds. Day buckets in UTC."""
    conn = _get_conn()
    since = _since_iso(days)
    klist = list(kinds)
    if not klist:
        return []
    qmarks = ",".join("?" for _ in klist)
    rows = conn.execute(
        f"""
        SELECT substr(ts, 1, 10) AS day, kind, COUNT(*) AS n
        FROM events
        WHERE kind IN ({qmarks}) AND ts >= ?
        GROUP BY day, kind
        ORDER BY day ASC
        """,
        (*klist, since),
    ).fetchall()
    return [{"day": r["day"], "kind": r["kind"], "n": int(r["n"])} for r in rows]


def top_routes(*, days: int | None, limit: int = 20) -> list[dict[str, Any]]:
    """Most-visited paths over the range, with count + avg duration_ms."""
    conn = _get_conn()
    since = _since_iso(days)
    rows = conn.execute(
        """
        SELECT route,
               COUNT(*) AS n,
               COALESCE(AVG(duration_ms), 0) AS avg_ms
        FROM events
        WHERE route IS NOT NULL
          AND kind IN ('pageview','api_call')
          AND ts >= ?
        GROUP BY route
        ORDER BY n DESC
        LIMIT ?
        """,
        (since, limit),
    ).fetchall()
    return [
        {"route": r["route"], "n": int(r["n"]), "avg_ms": float(r["avg_ms"] or 0.0)}
        for r in rows
    ]


def recent_errors(*, limit: int = 50) -> list[dict[str, Any]]:
    """Last *limit* error events, newest first."""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT ts, route, status, properties_json
        FROM events
        WHERE kind='error'
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def recent_jobs(*, limit: int = 50) -> list[dict[str, Any]]:
    """Last *limit* finished job events (grade + NL), newest first."""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT ts, kind, status, duration_ms, properties_json
        FROM events
        WHERE kind IN ('grade_job_finished','nl_job_finished')
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def session_count() -> int:
    conn = _get_conn()
    return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])


def device_breakdown() -> list[dict[str, Any]]:
    """Sessions grouped by (browser, os, is_mobile)."""
    conn = _get_conn()
    rows = conn.execute(
        """
        SELECT ua_browser, ua_os, is_mobile, COUNT(*) AS n
        FROM sessions
        GROUP BY ua_browser, ua_os, is_mobile
        ORDER BY n DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]
