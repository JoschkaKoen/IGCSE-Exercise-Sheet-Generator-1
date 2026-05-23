# -*- coding: utf-8 -*-
"""Internal usage-tracking package for the eXercise web app.

Tracks page views, sessions, job lifecycle, auth attempts, exam submissions,
and errors. AI cost data is NOT duplicated here — it's sourced from xScore's
``cost.json`` files and eXam's ``ai_calls`` table via :mod:`cost_overview`.

Public surface:

- :func:`track_event`, :func:`track_request_event` — record one event from
  any context / from a FastAPI route handler respectively.
- :func:`init_db` — create the analytics SQLite DB + schema. Call from
  :class:`fastapi.FastAPI` lifespan before yielding.
- :class:`AnalyticsMiddleware` — async HTTP middleware to register on the
  app. See its module docstring for placement guidance.
"""

from __future__ import annotations

from .events import track_event, track_request_event
from .middleware import AnalyticsMiddleware
from .store import init_db

__all__ = [
    "AnalyticsMiddleware",
    "init_db",
    "track_event",
    "track_request_event",
]
