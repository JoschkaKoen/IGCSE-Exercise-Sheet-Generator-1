# -*- coding: utf-8 -*-
"""Module-level singletons shared by ``app.py`` and route modules.

Both ``web/app.py`` and modules under ``web/routes/`` import the job store and
the background-task keepalive helper from here. Defining them in their own
module keeps imports linear (route modules don't have to import from app.py
and risk a partial-module cycle).
"""

from __future__ import annotations

import asyncio

from .jobs import JobStore

store: JobStore = JobStore()

_BACKGROUND_TASKS: set[asyncio.Task] = set()


def create_background_task(coro) -> asyncio.Task:
    """Schedule *coro* and keep a strong reference so it isn't GC'd mid-flight."""
    t = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t
