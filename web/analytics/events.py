# -*- coding: utf-8 -*-
"""Event-tracking helpers used by route handlers.

Two entry points:

- :func:`track_event` — low-level, works from any context (job-completion
  handler fired off the polling tick, background thread, etc.).
- :func:`track_request_event` — convenience for FastAPI route handlers; pulls
  ``session_id`` from ``request.state.session_id`` (populated by the analytics
  middleware), ``route`` from ``request.url.path``, ``method`` from
  ``request.method``.

Both swallow their own exceptions internally — analytics must never break a
request.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import Request

from . import store

_log = logging.getLogger(__name__)


def _props(properties: dict[str, Any] | None) -> str | None:
    if not properties:
        return None
    try:
        return json.dumps(properties, ensure_ascii=False, default=str)
    except Exception:
        _log.exception("analytics.events: properties not JSON-serialisable")
        return None


def track_event(
    kind: str,
    *,
    route: str | None = None,
    status: str | None = None,
    properties: dict[str, Any] | None = None,
    session_id: str | None = None,
    duration_ms: int | None = None,
    method: str | None = None,
    referrer: str | None = None,
    user_kind: str | None = None,
) -> None:
    """Append one analytics event. Never raises."""
    try:
        store.insert_event(
            kind=kind,
            method=method,
            session_id=session_id,
            route=route,
            referrer=referrer,
            status=status,
            user_kind=user_kind,
            properties_json=_props(properties),
            duration_ms=duration_ms,
        )
    except Exception:
        _log.exception("track_event failed (kind=%s)", kind)


def track_request_event(
    request: Request,
    kind: str,
    *,
    status: str | None = None,
    properties: dict[str, Any] | None = None,
    duration_ms: int | None = None,
) -> None:
    """Same as :func:`track_event`, but lifts session_id / route / method off *request*."""
    session_id = getattr(request.state, "session_id", None)
    user_kind = getattr(request.state, "user_kind", None)
    track_event(
        kind,
        route=request.url.path,
        method=request.method,
        session_id=session_id,
        status=status,
        properties=properties,
        duration_ms=duration_ms,
        user_kind=user_kind,
    )
