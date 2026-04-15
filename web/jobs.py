# -*- coding: utf-8 -*-
"""In-memory job store for async NL extraction runs."""

from __future__ import annotations

import dataclasses
import datetime
import threading
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobStatus(StrEnum):
    """Valid values for JobRecord.status and JobRecord.ranking_status."""
    PENDING = "pending"
    RUNNING = "running"
    DONE    = "done"
    FAILED  = "failed"
    SKIPPED = "skipped"


@dataclass
class JobRecord:
    """Single extraction job (poll until status is done or failed)."""

    id: str
    status: JobStatus
    error: str | None = None
    output_pdf: Path | None = None
    answers_pdf: Path | None = None
    exercise_4up_pdf: Path | None = None
    exercise_2up_pdf: Path | None = None
    answers_4up_pdf: Path | None = None
    answers_2up_pdf: Path | None = None
    ranking_pdf: Path | None = None
    ranking_status: JobStatus = JobStatus.PENDING
    ranking_log_line: str = ""
    log_line: str = ""
    overview: dict[str, Any] | None = None
    created_at: datetime.datetime = field(default_factory=datetime.datetime.now)


class JobStore:
    """Thread-safe UUID-keyed job registry (single-user local use)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}

    _JOB_TTL = datetime.timedelta(hours=24)

    def create(self) -> JobRecord:
        jid = str(uuid.uuid4())
        rec = JobRecord(id=jid, status=JobStatus.PENDING)
        with self._lock:
            # Evict jobs older than 24 h on each create to avoid unbounded growth.
            cutoff = datetime.datetime.now() - self._JOB_TTL
            stale = [k for k, j in self._jobs.items() if j.created_at < cutoff]
            for k in stale:
                del self._jobs[k]
            self._jobs[jid] = rec
        return rec

    def get(self, job_id: str) -> JobRecord | None:
        """Return a snapshot of the job record (not the live mutable object)."""
        with self._lock:
            j = self._jobs.get(job_id)
            return dataclasses.replace(j) if j is not None else None

    def set_status(self, job_id: str, status: JobStatus) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.status = status

    def set_log_line(self, job_id: str, line: str) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.log_line = line[:800] if line else ""

    def set_ranking_log_line(self, job_id: str, line: str) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.ranking_log_line = line[:800] if line else ""

    def set_ranking_status(self, job_id: str, status: JobStatus) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.ranking_status = status

    def try_start_ranking(self, job_id: str) -> bool:
        """Atomically transition ranking_status from PENDING → RUNNING.

        Returns True if the transition succeeded (caller should start the thread).
        Returns False if ranking is already in progress or done (caller should no-op).
        """
        with self._lock:
            j = self._jobs.get(job_id)
            if j is None or j.ranking_status != JobStatus.PENDING:
                return False
            j.ranking_status = JobStatus.RUNNING
            return True

    def set_ranking_result(self, job_id: str, ranking_pdf: Path) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.ranking_pdf = ranking_pdf
                j.ranking_status = JobStatus.DONE

    def fail(self, job_id: str, message: str) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.status = JobStatus.FAILED
                j.error = message

    def complete(
        self,
        job_id: str,
        output_pdf: Path,
        answers_pdf: Path | None,
        exercise_4up_pdf: Path | None = None,
        exercise_2up_pdf: Path | None = None,
        answers_4up_pdf: Path | None = None,
        answers_2up_pdf: Path | None = None,
        ranking_pdf: Path | None = None,
        overview: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.status = JobStatus.DONE
                j.output_pdf = output_pdf
                j.answers_pdf = answers_pdf
                j.exercise_4up_pdf = exercise_4up_pdf
                j.exercise_2up_pdf = exercise_2up_pdf
                j.answers_4up_pdf = answers_4up_pdf
                j.answers_2up_pdf = answers_2up_pdf
                j.ranking_pdf = ranking_pdf
                j.overview = overview
