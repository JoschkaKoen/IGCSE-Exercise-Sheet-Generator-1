# -*- coding: utf-8 -*-
"""In-memory job store for async NL extraction runs."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass
class JobRecord:
    """Single extraction job (poll until status is done or failed)."""

    id: str
    status: str  # pending | running | done | failed
    error: str | None = None
    output_pdf: Path | None = None
    answers_pdf: Path | None = None
    log_line: str = ""


class JobStore:
    """Thread-safe UUID-keyed job registry (single-user local use)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, JobRecord] = {}

    def create(self) -> JobRecord:
        jid = str(uuid.uuid4())
        rec = JobRecord(id=jid, status="pending")
        with self._lock:
            self._jobs[jid] = rec
        return rec

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def set_status(self, job_id: str, status: str) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.status = status

    def set_log_line(self, job_id: str, line: str) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.log_line = line[:800] if line else ""

    def fail(self, job_id: str, message: str) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.status = "failed"
                j.error = message

    def complete(
        self,
        job_id: str,
        output_pdf: Path,
        answers_pdf: Path | None,
    ) -> None:
        with self._lock:
            j = self._jobs.get(job_id)
            if j:
                j.status = "done"
                j.output_pdf = output_pdf
                j.answers_pdf = answers_pdf
