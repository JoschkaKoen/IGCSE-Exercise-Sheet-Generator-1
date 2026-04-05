# -*- coding: utf-8 -*-
"""FastAPI app: landing page, job API, exam library downloads."""

from __future__ import annotations

from eXercise.env_load import load_project_env

load_project_env()

import asyncio
import io
import zipfile
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from eXercise.config import EXAM_ROOT_BY_KEY
from eXercise.exceptions import ExtractionUserError
from eXercise.natural_language import MAX_NATURAL_LANGUAGE_INSTRUCTION_CHARS

from .auth_gate import (
    EXPECTED_CODE,
    apply_auth_cookie,
    codes_equal,
    enforce_login_rate_limit,
    normalize_submitted_code,
    parse_ask_login_mode,
    parse_login_disabled,
    request_is_authenticated,
)
from .jobs import JobStore
from .service import list_library_pdfs, run_nl_prompt_logged

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
STATIC_DIR = PACKAGE_DIR / "static"

app = FastAPI(title="eXercise")
store = JobStore()

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def site_access_gate(request: Request, call_next):
    """When login is enabled: signed cookie; /api/* (except login) returns 401 if missing."""
    if request.url.path.startswith("/static/"):
        return await call_next(request)

    login_disabled = parse_login_disabled(request)
    request.state.login_disabled = login_disabled
    if login_disabled:
        request.state.site_auth_ok = True
        request.state.ask_login_mode = False
    else:
        request.state.site_auth_ok = request_is_authenticated(request)
        request.state.ask_login_mode = parse_ask_login_mode(request)

    path = request.url.path
    if path.startswith("/api/") and path != "/api/auth/login":
        if not login_disabled and not request.state.site_auth_ok:
            return JSONResponse(
                status_code=401,
                content={"detail": "Login required"},
                headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
            )

    return await call_next(request)

ALLOWED_SUBJECTS = frozenset(EXAM_ROOT_BY_KEY.keys())


class CreateJobBody(BaseModel):
    prompt: str = Field(
        ...,
        min_length=1,
        max_length=MAX_NATURAL_LANGUAGE_INSTRUCTION_CHARS,
    )


class SiteLoginBody(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)


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
        main_pdf, ans_pdf, up4, up2, a4, a2, overview = await asyncio.to_thread(
            run_nl_prompt_logged, prompt, on_line
        )
        store.complete(job_id, main_pdf, ans_pdf, up4, up2, a4, a2, overview=overview)
    except ExtractionUserError as e:
        store.fail(job_id, str(e))
    except Exception as e:  # noqa: BLE001 — last-resort message for the UI
        store.fail(job_id, f"Unexpected error: {e}")


_HTML_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}


def _template_ctx(request: Request, **extra: object) -> dict[str, object]:
    login_disabled = getattr(request.state, "login_disabled", True)
    auth_ok = getattr(request.state, "site_auth_ok", False)
    ctx: dict[str, object] = {
        "request": request,
        "login_disabled": login_disabled,
        "needs_site_login": (not login_disabled) and (not auth_ok),
        "ask_login_mode": getattr(request.state, "ask_login_mode", False),
    }
    ctx.update(extra)
    return ctx


@app.post("/api/auth/login")
async def site_login(request: Request, body: SiteLoginBody) -> JSONResponse:
    """Set signed HttpOnly cookie after valid code. Session-style cookie when ASK_LOGIN or ?ask_login=."""
    if parse_login_disabled(request):
        raise HTTPException(status_code=403, detail="Login is disabled for this server")
    await enforce_login_rate_limit(request.client.host if request.client else "")
    normalized = normalize_submitted_code(body.code)
    if normalized is None or not codes_equal(normalized, EXPECTED_CODE):
        raise HTTPException(status_code=401, detail="Invalid code")
    response = JSONResponse(
        {"ok": True},
        headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
    )
    apply_auth_cookie(response, request, session_style=parse_ask_login_mode(request))
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        _template_ctx(request),
        headers=_HTML_NO_CACHE,
    )


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request) -> HTMLResponse:
    data = list_library_pdfs()
    return TEMPLATES.TemplateResponse(
        request,
        "library.html",
        _template_ctx(request, library=data),
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
        if rec.exercise_4up_pdf is not None:
            out["four_up_url"] = f"{base}/api/jobs/{job_id}/four-up"
        if rec.exercise_2up_pdf is not None:
            out["two_up_url"] = f"{base}/api/jobs/{job_id}/two-up"
        if rec.answers_4up_pdf is not None:
            out["answers_four_up_url"] = f"{base}/api/jobs/{job_id}/answers-four-up"
        if rec.answers_2up_pdf is not None:
            out["answers_two_up_url"] = f"{base}/api/jobs/{job_id}/answers-two-up"
        out["download_all_url"] = f"{base}/api/jobs/{job_id}/download-all"
        if rec.overview is not None:
            out["overview"] = rec.overview
    return JSONResponse(
        content=out,
        headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
    )


@app.get("/api/jobs/{job_id}/file")
async def download_job_file(job_id: str, inline: bool = Query(False)) -> FileResponse:
    rec = store.get(job_id)
    if rec is None or rec.status != "done" or rec.output_pdf is None:
        raise HTTPException(status_code=404, detail="Not available")
    path = rec.output_pdf
    disp = "inline" if inline else "attachment"
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/pdf",
        content_disposition_type=disp,
    )


@app.get("/api/jobs/{job_id}/answers")
async def download_job_answers(job_id: str, inline: bool = Query(False)) -> FileResponse:
    rec = store.get(job_id)
    if rec is None or rec.status != "done" or rec.answers_pdf is None:
        raise HTTPException(status_code=404, detail="Not available")
    path = rec.answers_pdf
    disp = "inline" if inline else "attachment"
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/pdf",
        content_disposition_type=disp,
    )


@app.get("/api/jobs/{job_id}/four-up")
async def download_job_four_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    rec = store.get(job_id)
    if rec is None or rec.status != "done" or rec.exercise_4up_pdf is None:
        raise HTTPException(status_code=404, detail="Not available")
    path = rec.exercise_4up_pdf
    disp = "inline" if inline else "attachment"
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/pdf",
        content_disposition_type=disp,
    )


@app.get("/api/jobs/{job_id}/two-up")
async def download_job_two_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    rec = store.get(job_id)
    if rec is None or rec.status != "done" or rec.exercise_2up_pdf is None:
        raise HTTPException(status_code=404, detail="Not available")
    path = rec.exercise_2up_pdf
    disp = "inline" if inline else "attachment"
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/pdf",
        content_disposition_type=disp,
    )


@app.get("/api/jobs/{job_id}/answers-four-up")
async def download_job_answers_four_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    rec = store.get(job_id)
    if rec is None or rec.status != "done" or rec.answers_4up_pdf is None:
        raise HTTPException(status_code=404, detail="Not available")
    path = rec.answers_4up_pdf
    disp = "inline" if inline else "attachment"
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/pdf",
        content_disposition_type=disp,
    )


@app.get("/api/jobs/{job_id}/answers-two-up")
async def download_job_answers_two_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    rec = store.get(job_id)
    if rec is None or rec.status != "done" or rec.answers_2up_pdf is None:
        raise HTTPException(status_code=404, detail="Not available")
    path = rec.answers_2up_pdf
    disp = "inline" if inline else "attachment"
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/pdf",
        content_disposition_type=disp,
    )


@app.get("/api/jobs/{job_id}/download-all")
async def download_job_all_zip(job_id: str) -> Response:
    """ZIP of exercise sheet plus mark scheme and n-up PDFs when present."""
    rec = store.get(job_id)
    if rec is None or rec.status != "done" or rec.output_pdf is None:
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
    zip_name = f"{rec.output_pdf.stem}_all.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@app.get("/api/library/{subject}/{filename}")
async def library_file(subject: str, filename: str) -> FileResponse:
    path = _validate_library_path(subject, filename)
    return FileResponse(path, filename=path.name, media_type="application/pdf")
