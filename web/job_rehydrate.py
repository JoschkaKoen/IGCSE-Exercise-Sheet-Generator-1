# -*- coding: utf-8 -*-
"""Reconstruct a completed NL job's ``JobRecord`` from its on-disk artifacts.

The in-memory :class:`web.jobs.JobStore` evicts jobs after 24 h and is empty
after a restart, but the durable ``jobs`` row (web/jobs_db.py) and the run's
output directory survive. Given a job id, this rebuilds the record the download
and preview routes need, so "past jobs" on the dashboard keep working long after
the live entry is gone.

Only ``done`` NL jobs are rehydratable; everything else returns ``None``.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from . import jobs_db
from .jobs import JobRecord, JobStatus

_log = logging.getLogger(__name__)

# Suffixes of the sibling PDFs derived from the main sheet's stem
# (see web/service.py). The main PDF is the only ``*.pdf`` in a run dir whose
# stem ends in none of these.
_SIBLING_SUFFIXES = ("_answers", "_4up", "_2up", "_answers_4up", "_answers_2up", "_ranking")


def _find_main_pdf(artifact_dir: Path) -> Path | None:
    """Locate the run's main exercise-sheet PDF inside *artifact_dir*.

    Primary: the dir is named after the sheet stem (with at most a `` 2``/`` 3``
    dedup suffix on the *dir* only — the file keeps the base stem), so
    ``<stem>.pdf`` is the main file. Correct even when the stem itself ends in a
    sibling word.
    Fallback: the single ``*.pdf`` whose stem ends in no sibling suffix — covers a
    legitimate stem ending in `` <digit>`` that the dedup-strip would mangle.
    """
    stem = re.sub(r" \d+$", "", artifact_dir.name)
    primary = artifact_dir / f"{stem}.pdf"
    if primary.is_file():
        return primary
    mains = [
        p for p in artifact_dir.glob("*.pdf")
        if not any(p.stem.endswith(s) for s in _SIBLING_SUFFIXES)
    ]
    return mains[0] if len(mains) == 1 else None


def rehydrate_done_job(job_id: str) -> JobRecord | None:
    """Rebuild a completed NL ``JobRecord`` from disk, or ``None`` if not possible."""
    row = jobs_db.get(job_id)
    if not row or row.get("kind") != "nl" or row.get("status") != "done":
        return None
    ad = row.get("artifact_dir")
    if not ad:
        return None
    artifact_dir = Path(ad)
    if not artifact_dir.is_dir():
        return None
    main = _find_main_pdf(artifact_dir)
    if main is None:
        return None
    stem = main.stem

    def sibling(suffix: str) -> Path | None:
        p = artifact_dir / f"{stem}{suffix}.pdf"
        return p if p.is_file() else None

    ranking = sibling("_ranking")

    overview = None
    ov_path = artifact_dir / "overview.json"
    if ov_path.is_file():
        try:
            overview = json.loads(ov_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _log.debug("overview.json unreadable for %s", job_id, exc_info=True)

    return JobRecord(
        id=job_id,
        status=JobStatus.DONE,
        user_id=row.get("user_id"),
        kind="nl",
        output_pdf=main,
        answers_pdf=sibling("_answers"),
        exercise_4up_pdf=sibling("_4up"),
        exercise_2up_pdf=sibling("_2up"),
        answers_4up_pdf=sibling("_answers_4up"),
        answers_2up_pdf=sibling("_answers_2up"),
        ranking_pdf=ranking,
        ranking_status=JobStatus.DONE if ranking else JobStatus.PENDING,
        overview=overview,
    )
