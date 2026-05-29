# -*- coding: utf-8 -*-
"""FastAPI HTTP middleware: page-view + error tracking + session cookie.

Registered in :mod:`web.app` *after* ``site_access_gate`` so it runs outermost
(Starlette LIFO ordering: middlewares added later wrap earlier ones). That
ordering means analytics sees the gate-issued 401 responses too.

Behaviour:

- Skip static / favicon / polling endpoints (those poll every 1–2s per active
  job per browser tab and would flood the events table).
- Assign a session cookie on first request (UUID), and reuse it thereafter.
- Set ``request.state.session_id`` so route handlers can call
  :func:`web.analytics.events.track_request_event` and get the session id for
  free.
- After ``await call_next(request)``: write a ``pageview`` (HTML routes) or
  ``api_call`` (``/api/*`` routes) event with method + duration + HTTP status.
- On unhandled exception: write an ``error`` event with class + truncated
  message snippet, then re-raise (FastAPI's normal error handling kicks in).

Writes happen **after** ``call_next`` so they never add latency before the
response starts streaming. SQLite WAL writes are sub-millisecond at this
volume.
"""

from __future__ import annotations

import logging
import re
import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from . import events, salt, sessions, store

_log = logging.getLogger(__name__)


# Paths that must NOT generate events on every request:
#   - static assets (/static/*, favicons): noise, no analytical value
#   - polling endpoints: poll every 1–2s per active job per tab
#
# The polling endpoints' exact paths come from a grep of:
#   - web/routes/nl_jobs.py:120  GET /api/jobs/{job_id}
#   - web/routes/grade_jobs.py:586 GET /api/grade/jobs/{job_id}/artifacts
# If a new polling endpoint is added, append it here.
_SKIP_PREFIXES: tuple[str, ...] = (
    "/static/",
    "/favicon.ico",
    "/apple-touch-icon",
)

_SKIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/api/jobs/[^/]+$"),                   # NL job poll
    re.compile(r"^/api/grade/jobs/[^/]+/artifacts$"),   # grade artifacts poll
)


def _should_skip(path: str) -> bool:
    if any(path.startswith(p) for p in _SKIP_PREFIXES):
        return True
    return any(p.match(path) for p in _SKIP_PATTERNS)


def _client_ip(request: Request) -> str:
    """Honour ``X-Forwarded-For`` so the IP behind nginx/traefik is the real client."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else ""


def _user_kind(request: Request) -> str:
    """Best-effort classification of the requester for the sessions row.

    site_access_gate has already populated ``request.state.site_auth_ok``;
    grade auth and eXam student auth are not modelled here for simplicity.
    """
    if getattr(request.state, "site_auth_ok", False):
        return "site-authed"
    if getattr(request.state, "login_disabled", True):
        return "anonymous"
    return "anonymous"


def _kind_for_path(path: str) -> str:
    return "api_call" if path.startswith("/api/") else "pageview"


class AnalyticsMiddleware(BaseHTTPMiddleware):
    """See module docstring."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Cheap path: skip lists short-circuit before any DB work or cookie ops.
        if _should_skip(path):
            return await call_next(request)

        # Speculative prefetch/prerender hits aren't real navigations — don't
        # mint a session or record a pageview. Chromium sets Sec-Purpose;
        # legacy engines use Purpose.
        sec_purpose = request.headers.get("sec-purpose", "").lower()
        purpose = request.headers.get("purpose", "").lower()
        if sec_purpose.startswith("prefetch") or purpose == "prefetch":
            return await call_next(request)

        t0 = time.perf_counter()

        # Mint (or read) the session id *before* call_next so route handlers
        # can pull it off request.state via track_request_event. Cookie write
        # itself is deferred until after call_next returns the real response.
        session_id, cookie_needed = sessions.read_or_mint_session_id(request)
        request.state.session_id = session_id

        ua_browser, ua_os, is_mobile = sessions.classify_user_agent(
            request.headers.get("user-agent", "")
        )
        ip_hash = salt.hash_ip(_client_ip(request))

        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 — must capture *anything* and re-raise
            duration_ms = int((time.perf_counter() - t0) * 1000)
            try:
                events.track_event(
                    "error",
                    route=path,
                    method=request.method,
                    session_id=session_id,
                    status="error",
                    properties={
                        "exception_class": type(exc).__name__,
                        "message": str(exc)[:200],
                    },
                    duration_ms=duration_ms,
                )
            except Exception:
                _log.exception("analytics middleware: error-event write failed")
            raise

        # Touch (or create) the session row now that we've classified the UA + IP.
        try:
            store.upsert_session(
                session_id=session_id,
                user_kind=_user_kind(request),
                ua_browser=ua_browser,
                ua_os=ua_os,
                is_mobile=is_mobile,
                ip_hash=ip_hash or None,
            )
        except Exception:
            _log.exception("analytics middleware: upsert_session failed")

        duration_ms = int((time.perf_counter() - t0) * 1000)
        referrer = request.headers.get("referer")
        try:
            events.track_event(
                _kind_for_path(path),
                route=path,
                method=request.method,
                session_id=session_id,
                referrer=referrer,
                status=str(response.status_code),
                duration_ms=duration_ms,
                user_kind=_user_kind(request),
            )
        except Exception:
            _log.exception("analytics middleware: pageview write failed")

        if cookie_needed:
            sessions.apply_session_cookie(response, request, session_id)

        return response
