# -*- coding: utf-8 -*-
"""Natural-language exercise-generation jobs.

Routes for the primary user flow: submit a prompt, poll for status, download
the resulting PDFs (exercise sheet, mark scheme, n-up variants, ranking).
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import threading
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from eXercise.difficulty_ranking import generate_difficulty_ranking
from eXercise.exceptions import ExtractionUserError
from eXercise.natural_language import MAX_NATURAL_LANGUAGE_INSTRUCTION_CHARS

from .._state import create_background_task, store
from .. import jobs_db
from ..job_rehydrate import rehydrate_done_job
from ..analytics import track_event, track_request_event
from ..analytics.cost_overview import rollup_from_cost_json
from ..jobs import JobRecord, JobStatus
from ..process_log import run_with_last_log_line
from ..service import run_nl_prompt_logged
from ..user_auth import current_user_id

router = APIRouter()


class CreateJobBody(BaseModel):
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=MAX_NATURAL_LANGUAGE_INSTRUCTION_CHARS,
    )


def _start_ranking_thread(job_id: str, main_pdf: Path, ans_pdf: Path | None) -> None:
    """Spawn the ranking background thread. Transition to RUNNING is done by the caller."""
    def _run() -> None:
        try:
            ranking_path = main_pdf.parent / f"{main_pdf.stem}_ranking{main_pdf.suffix}"

            def on_ranking_line(line: str) -> None:
                store.set_ranking_log_line(job_id, line)

            run_with_last_log_line(
                lambda: generate_difficulty_ranking(
                    exercise_pdf=main_pdf,
                    answer_pdf=ans_pdf if (ans_pdf and ans_pdf.exists()) else None,
                    out_path=main_pdf.parent,
                    name=main_pdf.stem,
                ),
                on_ranking_line,
            )
            if ranking_path.exists():
                store.set_ranking_result(job_id, ranking_path)
            else:
                store.set_ranking_status(job_id, JobStatus.SKIPPED)
        except Exception:  # noqa: BLE001
            logging.exception("Ranking failed for job %s", job_id)
            store.set_ranking_status(job_id, JobStatus.FAILED)

    threading.Thread(target=_run, daemon=True).start()


async def _run_job(job_id: str, prompt: str, session_id: str | None = None) -> None:
    store.set_status(job_id, JobStatus.RUNNING)
    rec0 = store.get(job_id)
    persist = bool(rec0 and rec0.user_id is not None)
    if persist:
        jobs_db.mark_running(job_id)
    # Set before the worker thread starts so the first poll always sees real text (not empty).
    store.set_log_line(job_id, "Resolving natural-language request…")

    def on_line(line: str) -> None:
        store.set_log_line(job_id, line)

    t_start = asyncio.get_event_loop().time()

    def _finish(status: str, main_pdf: Path | None) -> None:
        """Fire the `nl_job_finished` analytics event with cost rollup if available."""
        duration_ms = int((asyncio.get_event_loop().time() - t_start) * 1000)
        props: dict = {
            "job_id": job_id,
            "prompt": prompt[:100],  # truncate for storage / privacy
        }
        if main_pdf is not None:
            cost_json = main_pdf.parent / "ai_costs" / "cost.json"
            props.update(rollup_from_cost_json(cost_json))
        track_event(
            "nl_job_finished",
            status=status,
            session_id=session_id,
            duration_ms=duration_ms,
            properties=props,
        )

    try:
        main_pdf, ans_pdf, up4, up2, a4, a2, _ranking_pdf, overview = await asyncio.to_thread(
            run_nl_prompt_logged, prompt, on_line
        )
        store.complete(job_id, main_pdf, ans_pdf, up4, up2, a4, a2, ranking_pdf=None, overview=overview)
        if persist:
            cost_json = main_pdf.parent / "ai_costs" / "cost.json"
            rollup = rollup_from_cost_json(cost_json)
            jobs_db.mark_done(
                job_id,
                artifact_dir=main_pdf.parent,
                total_cost_rmb=float(rollup.get("ai_cost_rmb") or 0.0),
            )
            # Persist the nav overview so a rehydrated preview (after the live
            # record is evicted) can still build the per-exercise jump panel.
            # Best-effort: a write/serialize fault must never fail the run.
            try:
                (main_pdf.parent / "overview.json").write_text(
                    json.dumps(overview), encoding="utf-8"
                )
            except Exception:  # noqa: BLE001 — best-effort persistence
                logging.debug("overview.json write failed for %s", job_id, exc_info=True)
        _finish("ok", main_pdf)
        # Ranking is now on-demand: started only when the user clicks the ranking button.

    except ExtractionUserError as e:
        store.fail(job_id, str(e))
        if persist:
            jobs_db.mark_failed(job_id, error=str(e))
        _finish("fail", None)
    except Exception as e:  # noqa: BLE001 — last-resort message for the UI
        logging.exception("NL job %s failed", job_id)
        store.fail(job_id, f"Unexpected error: {e}")
        if persist:
            jobs_db.mark_failed(job_id, error=f"Unexpected error: {e}")
        _finish("error", None)


def resolve_job(job_id: str) -> JobRecord | None:
    """In-memory job, or one rehydrated from disk + adopted back into the store.

    Live jobs hit the store directly. For a job evicted by the 24 h TTL or lost
    on restart, rebuild it from its on-disk artifacts (web/job_rehydrate.py) and
    adopt it so downloads, preview, and on-demand ranking all work again. Returns
    None only when neither the store nor disk can produce the job.
    """
    rec = store.get(job_id)
    if rec is not None:
        return rec
    reh = rehydrate_done_job(job_id)
    if reh is None:
        return None
    store.put_if_absent(reh)      # adopt → subsequent calls hit the store
    return store.get(job_id)      # return a consistent snapshot


def _pdf_file_response(rec: JobRecord | None, field: str, inline: bool) -> FileResponse:
    """Return a FileResponse for a PDF field on a completed JobRecord, or raise 404."""
    if rec is None or rec.status != JobStatus.DONE:
        raise HTTPException(status_code=404, detail="Not available")
    path: Path | None = getattr(rec, field, None)
    if path is None:
        raise HTTPException(status_code=404, detail="Not available")
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/pdf",
        content_disposition_type="inline" if inline else "attachment",
    )


@router.post("/api/jobs")
async def create_job(body: CreateJobBody, request: Request) -> dict[str, str]:
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="prompt is required")
    user_id = current_user_id(request)
    job = store.create(user_id=user_id, kind="nl")
    if user_id is not None:
        jobs_db.insert(job_id=job.id, user_id=user_id, kind="nl", title=prompt[:80])
    session_id = getattr(request.state, "session_id", None)
    track_request_event(
        request, "nl_job_started",
        properties={"job_id": job.id, "prompt": prompt[:100]},
    )
    create_background_task(_run_job(job.id, prompt, session_id=session_id))
    return {"id": job.id}


_LOG_LINES_PER_RESPONSE_CAP = 2000


def _job_asset_path(job_id: str, suffix: str) -> str:
    """Same-origin relative path for a job artifact (avoids mixed-content when behind TLS proxy)."""
    return f"/api/jobs/{job_id}/{suffix}"


@router.get("/api/jobs/{job_id}")
async def job_status(
    job_id: str,
    since: int = Query(0, ge=0),
) -> JSONResponse:
    """Job status for polling; ``log_line`` is updated live (avoid caching in the browser).

    ``?since=N`` returns ``log_lines`` after index N (capped at
    ``_LOG_LINES_PER_RESPONSE_CAP`` per response). ``log_offset`` is the index
    the client should send back as ``?since=`` on its next poll.
    """
    rec = resolve_job(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Job not found")
    total_lines = len(rec.log_lines)
    start = min(since, total_lines)
    end = min(start + _LOG_LINES_PER_RESPONSE_CAP, total_lines)
    out: dict = {
        "status": rec.status,
        "error": rec.error,
        "log_line": rec.log_line or "",
        "log_lines": rec.log_lines[start:end],
        "log_offset": end,
        "steps": [
            {
                "num": s.num,
                "name": s.name,
                "status": s.status,
                "elapsed_s": s.elapsed_s,
                "section": s.section,
            }
            for s in rec.steps
        ],
    }
    if rec.status == JobStatus.DONE and rec.output_pdf is not None:
        out["download_url"] = _job_asset_path(job_id, "file")
        if rec.answers_pdf is not None:
            out["answers_url"] = _job_asset_path(job_id, "answers")
        if rec.exercise_4up_pdf is not None:
            out["four_up_url"] = _job_asset_path(job_id, "four-up")
        if rec.exercise_2up_pdf is not None:
            out["two_up_url"] = _job_asset_path(job_id, "two-up")
        if rec.answers_4up_pdf is not None:
            out["answers_four_up_url"] = _job_asset_path(job_id, "answers-four-up")
        if rec.answers_2up_pdf is not None:
            out["answers_two_up_url"] = _job_asset_path(job_id, "answers-two-up")
        if rec.ranking_pdf is not None:
            out["ranking_url"] = _job_asset_path(job_id, "ranking")
        out["ranking_status"] = rec.ranking_status
        out["ranking_log_line"] = rec.ranking_log_line
        out["download_all_url"] = _job_asset_path(job_id, "download-all")
        cost_json_path = rec.output_pdf.parent / "ai_costs" / "cost.json"
        if cost_json_path.is_file():
            out["cost_json_url"] = _job_asset_path(job_id, "cost.json")
            out["cost_md_url"] = _job_asset_path(job_id, "cost.md")
        if rec.overview is not None:
            out["overview"] = rec.overview
    return JSONResponse(
        content=out,
        headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
    )


@router.get("/api/jobs/{job_id}/file")
async def download_job_file(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(resolve_job(job_id), "output_pdf", inline)


@router.get("/api/jobs/{job_id}/answers")
async def download_job_answers(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(resolve_job(job_id), "answers_pdf", inline)


@router.get("/api/jobs/{job_id}/four-up")
async def download_job_four_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(resolve_job(job_id), "exercise_4up_pdf", inline)


@router.get("/api/jobs/{job_id}/two-up")
async def download_job_two_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(resolve_job(job_id), "exercise_2up_pdf", inline)


@router.get("/api/jobs/{job_id}/answers-four-up")
async def download_job_answers_four_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(resolve_job(job_id), "answers_4up_pdf", inline)


@router.get("/api/jobs/{job_id}/answers-two-up")
async def download_job_answers_two_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(resolve_job(job_id), "answers_2up_pdf", inline)


@router.post("/api/jobs/{job_id}/ranking/start")
async def start_job_ranking(job_id: str) -> JSONResponse:
    """Start the ranking background thread on demand (idempotent if already started)."""
    job = resolve_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status != JobStatus.DONE:
        raise HTTPException(status_code=400, detail="Job not complete")
    if not job.output_pdf:
        raise HTTPException(status_code=400, detail="No output PDF")
    output_pdf = job.output_pdf
    answers_pdf = job.answers_pdf
    # Atomically check+transition PENDING → RUNNING to prevent duplicate threads.
    if not store.try_start_ranking(job_id):
        return JSONResponse({"ok": True})  # already started or done
    _start_ranking_thread(job_id, output_pdf, answers_pdf)
    return JSONResponse({"ok": True})


@router.get("/api/jobs/{job_id}/ranking")
async def download_job_ranking(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(resolve_job(job_id), "ranking_pdf", inline)


def _cost_file_response(job_id: str, name: str, media_type: str, inline: bool) -> FileResponse:
    """Serve ``output/<stem>/ai_costs/<name>`` for a completed NL job, or 404."""
    rec = resolve_job(job_id)
    if rec is None or rec.status != JobStatus.DONE or rec.output_pdf is None:
        raise HTTPException(status_code=404, detail="Not available")
    p = rec.output_pdf.parent / "ai_costs" / name
    if not p.is_file():
        raise HTTPException(status_code=404, detail="Cost summary not available")
    return FileResponse(
        p,
        filename=p.name,
        media_type=media_type,
        content_disposition_type="inline" if inline else "attachment",
    )


@router.get("/api/jobs/{job_id}/cost.json")
async def download_job_cost_json(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _cost_file_response(job_id, "cost.json", "application/json", inline)


@router.get("/api/jobs/{job_id}/cost.md")
async def download_job_cost_md(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _cost_file_response(job_id, "cost.md", "text/markdown", inline)


@router.get("/api/jobs/{job_id}/download-all")
async def download_job_all_zip(job_id: str) -> Response:
    """ZIP of exercise sheet plus mark scheme, n-up PDFs, and AI cost summary when present."""
    rec = resolve_job(job_id)
    if rec is None or rec.status != JobStatus.DONE or rec.output_pdf is None:
        raise HTTPException(status_code=404, detail="Not available")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(rec.output_pdf, arcname=rec.output_pdf.name)
        if rec.answers_pdf is not None:
            zf.write(rec.answers_pdf, arcname=rec.answers_pdf.name)
        if rec.exercise_4up_pdf is not None:
            zf.write(rec.exercise_4up_pdf, arcname=rec.exercise_4up_pdf.name)
        if rec.exercise_2up_pdf is not None:
            zf.write(rec.exercise_2up_pdf, arcname=rec.exercise_2up_pdf.name)
        if rec.answers_4up_pdf is not None:
            zf.write(rec.answers_4up_pdf, arcname=rec.answers_4up_pdf.name)
        if rec.answers_2up_pdf is not None:
            zf.write(rec.answers_2up_pdf, arcname=rec.answers_2up_pdf.name)
        if rec.ranking_pdf is not None:
            zf.write(rec.ranking_pdf, arcname=rec.ranking_pdf.name)
        cost_dir = rec.output_pdf.parent / "ai_costs"
        for cost_file in ("cost.json", "cost.md"):
            cp = cost_dir / cost_file
            if cp.is_file():
                zf.write(cp, arcname=f"ai_costs/{cost_file}")
    zip_name = f"{rec.output_pdf.stem}_all.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )
