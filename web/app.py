# -*- coding: utf-8 -*-
"""FastAPI app: landing page, job API, exam library downloads."""

from __future__ import annotations

from eXercise.env_load import load_project_env

load_project_env()

import asyncio
from contextlib import asynccontextmanager
import io
import threading
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from eXercise.config import EXAM_ROOT_BY_KEY
from eXercise.difficulty_ranking import generate_difficulty_ranking
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
from .grade_service import run_scan_pipeline_logged
from .jobs import JobStore
from .process_log import run_with_last_log_line
from .service import list_library_pdfs, run_nl_prompt_logged

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
STATIC_DIR = PACKAGE_DIR / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    list_library_pdfs()  # pre-warm PDF index cache so the first page load is instant
    yield


app = FastAPI(title="eXercise", lifespan=_lifespan)
store = JobStore()

class NoCacheStaticFiles(StaticFiles):
    """StaticFiles subclass that disables browser caching (dev only)."""

    async def __call__(self, scope, receive, send):
        async def send_with_no_cache(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                # Remove any existing cache headers
                headers = [(k, v) for k, v in headers if k.lower() not in (b"cache-control", b"etag", b"last-modified")]
                headers.append((b"cache-control", b"no-store, no-cache, must-revalidate, max-age=0"))
                message["headers"] = headers
            await send(message)

        await super().__call__(scope, receive, send_with_no_cache)


if STATIC_DIR.is_dir():
    app.mount("/static", NoCacheStaticFiles(directory=str(STATIC_DIR)), name="static")

_favicon_svg = STATIC_DIR / "favicon.svg"


@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico() -> FileResponse:
    """Browsers request /favicon.ico by default; serve the SVG with an .ico URL."""
    return FileResponse(_favicon_svg, media_type="image/svg+xml")


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def apple_touch_icon() -> FileResponse:
    """Silence 404s from Safari / iOS home-screen bookmark probes."""
    return FileResponse(_favicon_svg, media_type="image/svg+xml")


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
        main_pdf, ans_pdf, up4, up2, a4, a2, _ranking_pdf, overview = await asyncio.to_thread(
            run_nl_prompt_logged, prompt, on_line
        )
        # ranking_pdf from service is always None (run_ranking=False); ranking runs below
        store.complete(job_id, main_pdf, ans_pdf, up4, up2, a4, a2, ranking_pdf=None, overview=overview)

        def _run_ranking() -> None:
            try:
                store.set_ranking_status(job_id, "running")
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
                    store.set_ranking_status(job_id, "skipped")
            except Exception:  # noqa: BLE001
                store.set_ranking_status(job_id, "failed")

        threading.Thread(target=_run_ranking, daemon=True).start()

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


@app.get("/grade", response_class=HTMLResponse)
async def grade_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "grade.html",
        _template_ctx(request),
        headers=_HTML_NO_CACHE,
    )


@app.get("/library", response_class=HTMLResponse)
async def library_page(request: Request) -> HTMLResponse:
    data = list_library_pdfs()
    library_json = {k: v for k, v in data.items() if k != "physics"}
    return TEMPLATES.TemplateResponse(
        request,
        "library.html",
        _template_ctx(request, library=data, library_json=library_json),
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
        if rec.ranking_pdf is not None:
            out["ranking_url"] = f"{base}/api/jobs/{job_id}/ranking"
        out["ranking_status"] = rec.ranking_status
        out["ranking_log_line"] = rec.ranking_log_line
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


@app.get("/api/jobs/{job_id}/ranking")
async def download_job_ranking(job_id: str, inline: bool = Query(False)) -> FileResponse:
    rec = store.get(job_id)
    if rec is None or rec.status != "done" or rec.ranking_pdf is None:
        raise HTTPException(status_code=404, detail="Not available")
    path = rec.ranking_pdf
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
        if rec.ranking_pdf is not None:
            zf.write(rec.ranking_pdf, arcname=rec.ranking_pdf.name)
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


# ---------------------------------------------------------------------------
# Grade jobs — scan pipeline (steps 1, 3, 5–7)
# ---------------------------------------------------------------------------

_GRADE_UPLOADS_ROOT = Path("output") / "grade_uploads"


async def _run_grade_job(
    job_id: str,
    folder: Path,
    prompt: str | None,
) -> None:
    store.set_status(job_id, "running")
    store.set_log_line(job_id, "Starting scan pipeline…")

    def on_line(line: str) -> None:
        store.set_log_line(job_id, line)

    try:
        cleaned_pdf = await asyncio.to_thread(
            run_scan_pipeline_logged, folder, prompt, on_line
        )
        store.complete(job_id, output_pdf=cleaned_pdf, answers_pdf=None)
    except Exception as e:  # noqa: BLE001
        store.fail(job_id, f"Scan pipeline error: {e}")


@app.post("/api/grade/jobs")
async def create_grade_job(
    exam_scans: UploadFile = File(...),
    student_list: UploadFile = File(...),
    empty_exam: UploadFile | None = File(None),
    answer_sheet: UploadFile | None = File(None),
    prompt: str | None = Form(None),
) -> dict[str, str]:
    """Accept uploaded exam files, save them, and launch an xScore pipeline job."""
    upload_id = str(uuid.uuid4())
    folder = _GRADE_UPLOADS_ROOT / upload_id
    folder.mkdir(parents=True, exist_ok=True)

    # Save required files
    scan_path = folder / "scan.pdf"
    scan_path.write_bytes(await exam_scans.read())

    sl_suffix = Path(student_list.filename or "StudentList.xlsx").suffix or ".xlsx"
    sl_path = folder / f"StudentList{sl_suffix}"
    sl_path.write_bytes(await student_list.read())

    # Save optional files
    if empty_exam is not None and empty_exam.filename:
        (folder / "empty_exam.pdf").write_bytes(await empty_exam.read())
    if answer_sheet is not None and answer_sheet.filename:
        (folder / "answer_sheet.pdf").write_bytes(await answer_sheet.read())

    job = store.create()
    asyncio.create_task(_run_grade_job(job.id, folder, prompt or None))
    return {"id": job.id}
