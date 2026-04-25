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
class StepRecord:
    """Progress record for a single pipeline step inside a grade job."""
    num: int
    name: str
    status: JobStatus = JobStatus.PENDING
    elapsed_s: float | None = None


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
    log_lines: list[str] = field(default_factory=list)
    overview: dict[str, Any] | None = None
    steps: list[StepRecord] = field(default_factory=list)
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
            if j is None:
                return None
            # dataclasses.replace is shallow — copy log_lines so callers don't
            # walk a list being appended to from the worker thread.
            return dataclasses.replace(j, log_lines=list(j.log_lines))

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

    _LOG_LINES_SOFT_CAP = 5000

    def append_log_line(self, job_id: str, line: str) -> None:
        """Append a captured stdout line to the job's scrollback buffer.

        Soft-caps at ``_LOG_LINES_SOFT_CAP`` entries (drops oldest) so a runaway
        producer can't blow up memory.
        """
        if not line:
            return
        with self._lock:
            j = self._jobs.get(job_id)
            if not j:
                return
            j.log_lines.append(line[:2000])
            overflow = len(j.log_lines) - self._LOG_LINES_SOFT_CAP
            if overflow > 0:
                del j.log_lines[:overflow]

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

    def init_steps(self, job_id: str, step_defs: list[tuple[int, str]]) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.steps = [StepRecord(num=n, name=name) for n, name in step_defs]

    def _find_step(self, j: JobRecord, num: int) -> StepRecord | None:
        for s in j.steps:
            if s.num == num:
                return s
        return None

    def step_running(self, job_id: str, num: int) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                s = self._find_step(j, num)
                if s:
                    s.status = JobStatus.RUNNING

    def step_done(self, job_id: str, num: int, elapsed_s: float) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                s = self._find_step(j, num)
                if s:
                    s.status = JobStatus.DONE
                    s.elapsed_s = elapsed_s

    def step_failed(self, job_id: str, num: int, elapsed_s: float) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                s = self._find_step(j, num)
                if s:
                    s.status = JobStatus.FAILED
                    s.elapsed_s = elapsed_s

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
