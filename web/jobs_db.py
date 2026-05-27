# -*- coding: utf-8 -*-
"""Persistent per-user job ledger (SQLite, alongside the in-memory ``JobStore``).

``web.jobs.JobStore`` is the live-status cache (log lines, step states,
subprocess handles) with a 24 h eviction TTL. This module is the durable side:
one row per submitted NL or grade job, attributed to a user, with the artifact
directory and an AI-cost rollup read from ``cost.json`` on completion. Backs
the dashboard's "My exercise sheets" / "My grade jobs" tables.

Anonymous jobs (no logged-in user) skip every helper here — they live and die
in the in-memory store only. All helpers swallow ``sqlite3.Error`` silently:
a persistence fault must never break the live job flow.
"""

from __future__ import annotations

import datetime as _dt
import logging
import sqlite3
from pathlib import Path

from eXam.db import connect

_log = logging.getLogger(__name__)


def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def insert(*, job_id: str, user_id: int, kind: str, title: str) -> None:
    """Insert a pending job row. No-op if persistence fails."""
    try:
        with connect() as conn:
            conn.execute(
                "INSERT INTO jobs (id, user_id, kind, title, status, created_at) "
                "VALUES (?, ?, ?, ?, 'pending', ?)",
                (job_id, user_id, kind, title[:240], _now_iso()),
            )
    except sqlite3.Error:
        _log.debug("jobs_db.insert failed for %s", job_id, exc_info=True)


def set_artifact_dir(job_id: str, artifact_dir: Path, *, title: str | None = None) -> None:
    """Record where the job's downloadable artifacts live.

    Grade jobs use the optional *title* to upgrade the initial upload-folder
    name to the exam stem once xScore's locate_exam_folder step resolves it.
    """
    try:
        with connect() as conn:
            if title is not None:
                conn.execute(
                    "UPDATE jobs SET artifact_dir = ?, title = ? WHERE id = ?",
                    (str(artifact_dir), title[:240], job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET artifact_dir = ? WHERE id = ?",
                    (str(artifact_dir), job_id),
                )
    except sqlite3.Error:
        _log.debug("jobs_db.set_artifact_dir failed for %s", job_id, exc_info=True)


def mark_running(job_id: str) -> None:
    try:
        with connect() as conn:
            conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (job_id,))
    except sqlite3.Error:
        _log.debug("jobs_db.mark_running failed for %s", job_id, exc_info=True)


def mark_done(
    job_id: str,
    *,
    artifact_dir: Path | None = None,
    total_cost_rmb: float | None = None,
) -> None:
    try:
        with connect() as conn:
            if artifact_dir is not None:
                conn.execute(
                    "UPDATE jobs SET status='done', completed_at=?, artifact_dir=?, total_cost_rmb=? WHERE id = ?",
                    (_now_iso(), str(artifact_dir), total_cost_rmb, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status='done', completed_at=?, total_cost_rmb=? WHERE id = ?",
                    (_now_iso(), total_cost_rmb, job_id),
                )
    except sqlite3.Error:
        _log.debug("jobs_db.mark_done failed for %s", job_id, exc_info=True)


def mark_failed(job_id: str, *, error: str) -> None:
    try:
        with connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', completed_at=?, error=? WHERE id = ?",
                (_now_iso(), (error or "")[:2000], job_id),
            )
    except sqlite3.Error:
        _log.debug("jobs_db.mark_failed failed for %s", job_id, exc_info=True)


def list_for_user(user_id: int, *, kind: str, limit: int = 50) -> list[dict]:
    """Return recent jobs for the dashboard tables.

    Each row: id, kind, title, status, error, created_at, completed_at,
    artifact_dir, total_cost_rmb. ``artifact_exists`` is computed (boolean)
    so the template can mark expired artifacts.
    """
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, kind, title, status, error, created_at, completed_at, "
                "       artifact_dir, total_cost_rmb "
                "FROM jobs WHERE user_id = ? AND kind = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (user_id, kind, int(limit)),
            ).fetchall()
    except sqlite3.Error:
        return []
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        ad = d.get("artifact_dir")
        d["artifact_exists"] = bool(ad) and Path(ad).is_dir()
        out.append(d)
    return out


def total_cost_for_user(user_id: int, *, kind: str) -> float:
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(total_cost_rmb), 0.0) AS total "
                "FROM jobs WHERE user_id = ? AND kind = ?",
                (user_id, kind),
            ).fetchone()
    except sqlite3.Error:
        return 0.0
    return float(row["total"] or 0.0) if row else 0.0
