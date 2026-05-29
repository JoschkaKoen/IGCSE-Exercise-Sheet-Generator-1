# -*- coding: utf-8 -*-
"""Server-backed progress for the ``/code`` Python-lesson playground.

One JSON-blob row per (anonymous open-mode session, lesson) in the
``code_progress`` table (see ``eXam/db.py`` migration 2→3). The blob is the same
shape the browser keeps in ``localStorage``::

    {"revealed": <int>, "tasks": {<taskId>: {"done": bool, "attempts": int, "ts": str}}}

Identity mirrors the ``open_*`` tables: rows are keyed by ``session_id`` and
carry a nullable ``user_id`` that ``web.routes.account.link_open_session`` fills
in on login, so a learner's progress follows them across devices once they have
an account. Reads therefore prefer ``user_id`` (merging every session that user
has touched) and fall back to the bare session.

All merges are **monotonic** — ``revealed`` only ever rises (``max``) and a task
``done`` flag only ever sets (``or``) — so a stale-cache client can never lower
what the server already knows.
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from eXam.db import connect

_DEFAULT: dict[str, Any] = {"revealed": 1, "tasks": {}}


def _now() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def _parse_state(text: str | None) -> dict[str, Any]:
    """Coerce a stored ``state`` TEXT column to the canonical dict shape."""
    try:
        v = json.loads(text or "")
    except (ValueError, TypeError):
        return {"revealed": 1, "tasks": {}}
    if not isinstance(v, dict):
        return {"revealed": 1, "tasks": {}}
    revealed = v.get("revealed")
    tasks = v.get("tasks")
    return {
        "revealed": int(revealed) if isinstance(revealed, (int, float)) and revealed >= 1 else 1,
        "tasks": tasks if isinstance(tasks, dict) else {},
    }


def _merge_task(prev: dict[str, Any] | None, incoming: dict[str, Any] | None) -> dict[str, Any]:
    prev = prev or {}
    incoming = incoming or {}
    return {
        "done": bool(prev.get("done")) or bool(incoming.get("done")),
        "attempts": max(int(prev.get("attempts", 0) or 0), int(incoming.get("attempts", 0) or 0)),
        "ts": max(str(prev.get("ts") or ""), str(incoming.get("ts") or "")),
    }


def load_progress(
    session_id: str | None, user_id: int | None, slug: str, nn: str
) -> dict[str, Any]:
    """Return the merged ``{revealed, tasks}`` for this learner + lesson.

    When *user_id* is set, merge across **every** row that user has (cross-device);
    otherwise read the single anonymous-session row. Defaults to
    ``{"revealed": 1, "tasks": {}}`` when nothing is stored.
    """
    if user_id is None and not session_id:
        return {"revealed": 1, "tasks": {}}
    with connect() as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT state FROM code_progress WHERE user_id=? AND slug=? AND nn=?",
                (user_id, slug, nn),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT state FROM code_progress WHERE session_id=? AND slug=? AND nn=?",
                (session_id, slug, nn),
            ).fetchall()
    if not rows:
        return {"revealed": 1, "tasks": {}}
    merged: dict[str, Any] = {"revealed": 1, "tasks": {}}
    for r in rows:
        st = _parse_state(r["state"])
        merged["revealed"] = max(merged["revealed"], st["revealed"])
        for tid, t in st["tasks"].items():
            merged["tasks"][str(tid)] = _merge_task(merged["tasks"].get(str(tid)), t)
    return merged


def save_progress(
    session_id: str,
    user_id: int | None,
    slug: str,
    nn: str,
    *,
    revealed: int | None = None,
    task: dict[str, Any] | None = None,
) -> None:
    """Monotonically merge *revealed* and/or one *task* into this session's row.

    ``task`` is ``{"id": str, "done": bool, "attempts": int}``. The read-modify-write
    runs inside ``BEGIN IMMEDIATE`` so two rapid POSTs for the same lesson can't
    race and drop an update. ``user_id`` is stamped (never cleared) so a later
    login backfill or a logged-in write attributes the row.
    """
    now = _now()
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT state FROM code_progress WHERE session_id=? AND slug=? AND nn=?",
                (session_id, slug, nn),
            ).fetchone()
            state = _parse_state(row["state"]) if row else {"revealed": 1, "tasks": {}}
            if revealed is not None:
                state["revealed"] = max(state["revealed"], int(revealed))
            if task is not None and task.get("id"):
                tid = str(task["id"])
                # Cap distinct tasks per row — a real lesson has a handful; this
                # bounds the blob against an attacker POSTing endless unique ids.
                if tid in state["tasks"] or len(state["tasks"]) < 200:
                    state["tasks"][tid] = _merge_task(
                        state["tasks"].get(tid),
                        {"done": task.get("done"), "attempts": task.get("attempts"), "ts": now},
                    )
            payload = json.dumps(state, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO code_progress (session_id, user_id, slug, nn, state, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, slug, nn) DO UPDATE SET
                    state      = excluded.state,
                    user_id    = COALESCE(excluded.user_id, code_progress.user_id),
                    updated_at = excluded.updated_at
                """,
                (session_id, user_id, slug, nn, payload, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
