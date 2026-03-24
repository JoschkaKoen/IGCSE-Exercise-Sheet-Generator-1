# -*- coding: utf-8 -*-
"""FastAPI app: landing page, job API, exam library downloads."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from extract_exercises.config import EXAM_ROOT_BY_KEY
from extract_exercises.exceptions import ExtractionUserError

from .jobs import JobStore
from .service import list_library_pdfs, run_nl_prompt_logged

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
STATIC_DIR = PACKAGE_DIR / "static"

app = FastAPI(title="Exercise Sheet Generator")
store = JobStore()

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

ALLOWED_SUBJECTS = frozenset(EXAM_ROOT_BY_KEY.keys())


class CreateJobBody(BaseModel):
    prompt: str = Field(..., min_length=1)


def _validate_library_path(subject: str, filename: str) -> Path:
    if subject not in ALLOWED_SUBJECTS:
        raise HTTPException(status_code=404, detail="Unknown subject")
    if "/" in filename or "\\" in filename or filename.strip() != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    safe = Path(filename).name
    if safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    root = EXAM_ROOT_BY_KEY[subject].resolve()
    path = (root / safe).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path


async def _run_job(job_id: str, prompt: str) -> None:
    store.set_status(job_id, "running")
    # Set before the worker thread starts so the first poll always sees real text (not empty).
    store.set_log_line(job_id, "Resolving natural-language request…")

    def on_line(line: str) -> None:
        store.set_log_line(job_id, line)

    try:
        main_pdf, ans_pdf = await asyncio.to_thread(run_nl_prompt_logged, prompt, on_line)
        store.complete(job_id, main_pdf, ans_pdf)
    except ExtractionUserError as e:
        store.fail(job_id, str(e))
    except Exception as e:  # noqa: BLE001 — last-resort message for the UI
        store.fail(job_id, f"Unexpected error: {e}")


_HTML_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        "index.html",
        {"request": request},
        headers=_HTML_NO_CACHE,
    )


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request) -> HTMLResponse:
    data = list_library_pdfs()
    return TEMPLATES.TemplateResponse(
        "library.html",
        {"request": request, "library": data},
        headers=_HTML_NO_CACHE,
    )


@app.post("/api/jobs")
async def create_job(body: CreateJobBody) -> dict[str, str]:
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="prompt is required")
    job = store.create()
    asyncio.create_task(_run_job(job.id, prompt))
    return {"id": job.id}


@app.get("/api/jobs/{job_id}")
async def job_status(request: Request, job_id: str) -> dict:
    """Job status for polling; ``log_line`` is updated live (avoid caching in the browser)."""
    rec = store.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Job not found")
    base = str(request.base_url).rstrip("/")
    out: dict = {
        "status": rec.status,
        "error": rec.error,
        "log_line": rec.log_line or "",
    }
    if rec.status == "done" and rec.output_pdf is not None:
        out["download_url"] = f"{base}/api/jobs/{job_id}/file"
        if rec.answers_pdf is not None:
            out["answers_url"] = f"{base}/api/jobs/{job_id}/answers"
    return JSONResponse(
        content=out,
        headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
    )


@app.get("/api/jobs/{job_id}/file")
async def download_job_file(job_id: str) -> FileResponse:
    rec = store.get(job_id)
    if rec is None or rec.status != "done" or rec.output_pdf is None:
        raise HTTPException(status_code=404, detail="Not available")
    path = rec.output_pdf
    return FileResponse(path, filename=path.name, media_type="application/pdf")


@app.get("/api/jobs/{job_id}/answers")
async def download_job_answers(job_id: str) -> FileResponse:
    rec = store.get(job_id)
    if rec is None or rec.status != "done" or rec.answers_pdf is None:
        raise HTTPException(status_code=404, detail="Not available")
    path = rec.answers_pdf
    return FileResponse(path, filename=path.name, media_type="application/pdf")


@app.get("/api/library/{subject}/{filename}")
async def library_file(subject: str, filename: str) -> FileResponse:
    path = _validate_library_path(subject, filename)
    return FileResponse(path, filename=path.name, media_type="application/pdf")
