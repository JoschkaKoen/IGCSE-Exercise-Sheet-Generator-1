# -*- coding: utf-8 -*-
"""FastAPI app: landing page, job API, exam library downloads."""

from __future__ import annotations

from eXercise.env_load import load_project_env

load_project_env()

import asyncio
from contextlib import asynccontextmanager
import datetime
import io
import logging
import os
import threading
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
from .grade_service import run_full_pipeline_logged
from .jobs import JobRecord, JobStatus, JobStore
from .process_log import run_with_last_log_line
from .service import invalidate_library_cache, list_library_pdfs, run_nl_prompt_logged

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
async def favicon_ico() -> Response:
    """Browsers request /favicon.ico by default; serve the SVG with an .ico URL."""
    if not Path(_favicon_svg).exists():
        return Response(status_code=404)
    return FileResponse(_favicon_svg, media_type="image/svg+xml")


@app.get("/apple-touch-icon.png", include_in_schema=False)
@app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
async def apple_touch_icon() -> Response:
    """Silence 404s from Safari / iOS home-screen bookmark probes."""
    if not Path(_favicon_svg).exists():
        return Response(status_code=404)
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


async def _run_job(job_id: str, prompt: str) -> None:
    store.set_status(job_id, JobStatus.RUNNING)
    # Set before the worker thread starts so the first poll always sees real text (not empty).
    store.set_log_line(job_id, "Resolving natural-language request…")

    def on_line(line: str) -> None:
        store.set_log_line(job_id, line)

    try:
        main_pdf, ans_pdf, up4, up2, a4, a2, _ranking_pdf, overview = await asyncio.to_thread(
            run_nl_prompt_logged, prompt, on_line
        )
        store.complete(job_id, main_pdf, ans_pdf, up4, up2, a4, a2, ranking_pdf=None, overview=overview)
        # Ranking is now on-demand: started only when the user clicks the ranking button.

    except ExtractionUserError as e:
        store.fail(job_id, str(e))
    except Exception as e:  # noqa: BLE001 — last-resort message for the UI
        logging.exception("NL job %s failed", job_id)
        store.fail(job_id, f"Unexpected error: {e}")


_HTML_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}

# Keep strong references to background tasks so they aren't GC'd mid-flight.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _create_background_task(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(t)
    t.add_done_callback(_BACKGROUND_TASKS.discard)
    return t


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
    # X-Forwarded-For (set by nginx/traefik) gives the real client IP behind a proxy.
    xff = request.headers.get("x-forwarded-for", "")
    client_ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")
    await enforce_login_rate_limit(client_ip)
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
    # "physics" is excluded from library_json because it uses a different
    # folder layout (per-topic sub-folders) that the library template cannot
    # render yet.  data (the full dict) is still passed for server-side use.
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
    _create_background_task(_run_job(job.id, prompt))
    return {"id": job.id}


@app.get("/api/jobs/{job_id}")
async def job_status(request: Request, job_id: str) -> JSONResponse:
    """Job status for polling; ``log_line`` is updated live (avoid caching in the browser)."""
    rec = store.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Job not found")
    base = str(request.base_url).rstrip("/")
    out: dict = {
        "status": rec.status,
        "error": rec.error,
        "log_line": rec.log_line or "",
        "steps": [
            {"num": s.num, "name": s.name, "status": s.status, "elapsed_s": s.elapsed_s}
            for s in rec.steps
        ],
    }
    if rec.status == JobStatus.DONE and rec.output_pdf is not None:
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


@app.get("/api/jobs/{job_id}/file")
async def download_job_file(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(store.get(job_id), "output_pdf", inline)


@app.get("/api/jobs/{job_id}/answers")
async def download_job_answers(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(store.get(job_id), "answers_pdf", inline)


@app.get("/api/jobs/{job_id}/four-up")
async def download_job_four_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(store.get(job_id), "exercise_4up_pdf", inline)


@app.get("/api/jobs/{job_id}/two-up")
async def download_job_two_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(store.get(job_id), "exercise_2up_pdf", inline)


@app.get("/api/jobs/{job_id}/answers-four-up")
async def download_job_answers_four_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(store.get(job_id), "answers_4up_pdf", inline)


@app.get("/api/jobs/{job_id}/answers-two-up")
async def download_job_answers_two_up(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(store.get(job_id), "answers_2up_pdf", inline)


@app.post("/api/jobs/{job_id}/ranking/start")
async def start_job_ranking(job_id: str) -> JSONResponse:
    """Start the ranking background thread on demand (idempotent if already started)."""
    job = store.get(job_id)
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


@app.get("/api/jobs/{job_id}/ranking")
async def download_job_ranking(job_id: str, inline: bool = Query(False)) -> FileResponse:
    return _pdf_file_response(store.get(job_id), "ranking_pdf", inline)


@app.get("/api/jobs/{job_id}/download-all")
async def download_job_all_zip(job_id: str) -> Response:
    """ZIP of exercise sheet plus mark scheme and n-up PDFs when present."""
    rec = store.get(job_id)
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
    zip_name = f"{rec.output_pdf.stem}_all.zip"
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@app.post("/api/admin/library/refresh")
async def refresh_library_cache() -> dict[str, str]:
    """Invalidate the library PDF index cache so the next page load rescans the disk."""
    invalidate_library_cache()
    return {"status": "ok"}


@app.get("/api/library/{subject}/{filename}")
async def library_file(subject: str, filename: str) -> FileResponse:
    path = _validate_library_path(subject, filename)
    return FileResponse(path, filename=path.name, media_type="application/pdf")


# ---------------------------------------------------------------------------
# Grade jobs — full xScore pipeline (steps 1–14)
# ---------------------------------------------------------------------------

_GRADE_UPLOADS_ROOT = Path(__file__).parent.parent / "output" / "xscore" / "grade_uploads"

_GRADE_STEPS = [
    (1,  "Parse grading instructions"),
    (2,  "Folder from upload"),
    (3,  "Load student roster"),
    (4,  "Detect blank pages"),
    (5,  "Autorotate"),
    (6,  "Deskew"),
    (7,  "Exam geometry + assign pages"),
    (8,  "Parse exam PDF"),
    (9,  "Parse mark scheme"),
    (10, "Build scaffold"),
    (11, "Build blueprints"),
    (12, "AI marking"),
    (13, "Compile reports"),
    (14, "Timing summary"),
]


async def _run_grade_job(
    job_id: str,
    folder: Path,
    prompt: str | None,
) -> None:
    store.set_status(job_id, JobStatus.RUNNING)
    store.init_steps(job_id, _GRADE_STEPS)
    store.set_log_line(job_id, "Starting pipeline…")

    def on_line(line: str) -> None:
        store.set_log_line(job_id, line)

    def on_step(num: int, event: str, elapsed_s: float | None) -> None:
        if event == "running":
            store.step_running(job_id, num)
        elif event == "done":
            store.step_done(job_id, num, elapsed_s or 0.0)
        elif event == "failed":
            store.step_failed(job_id, num, elapsed_s or 0.0)

    try:
        cleaned_pdf, artifact_dir = await asyncio.to_thread(
            run_full_pipeline_logged, folder, prompt, on_line, on_step
        )
        from xscore.shared.exam_paths import artifact_class_report_pdf_path
        class_report_pdf = artifact_class_report_pdf_path(artifact_dir)
        store.complete(
            job_id,
            output_pdf=cleaned_pdf,
            answers_pdf=class_report_pdf if class_report_pdf.is_file() else None,
        )
    except Exception as e:  # noqa: BLE001
        logging.exception("Grade pipeline failed for job %s", job_id)
        store.fail(job_id, f"Pipeline error: {e}")


@app.post("/api/grade/jobs")
async def create_grade_job(
    exam_scans: UploadFile = File(...),
    student_list: UploadFile = File(...),
    empty_exam: UploadFile = File(...),
    answer_sheet: UploadFile = File(...),
    prompt: str | None = Form(None),
) -> dict[str, str]:
    """Accept uploaded exam files, save them, and launch an xScore pipeline job."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # 8 random bytes = 2^64 space; retry on collision (astronomically rare)
    for _ in range(5):
        upload_id = f"{ts}_{os.urandom(8).hex()}"
        folder = _GRADE_UPLOADS_ROOT / upload_id
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=False)
            break
    else:
        raise RuntimeError("Could not create a unique upload directory")

    _MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB

    async def _read_limited(upload: UploadFile, name: str) -> bytes:
        data = await upload.read()
        if len(data) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{name} exceeds the {_MAX_UPLOAD_BYTES // (1024*1024)} MB upload limit",
            )
        return data

    # Save required files
    scan_path = folder / "scan.pdf"
    scan_path.write_bytes(await _read_limited(exam_scans, "exam_scans"))

    sl_suffix = Path(student_list.filename or "StudentList.xlsx").suffix or ".xlsx"
    sl_path = folder / f"StudentList{sl_suffix}"
    sl_path.write_bytes(await _read_limited(student_list, "student_list"))

    # Save required files (all four are now mandatory)
    (folder / "empty_exam.pdf").write_bytes(await _read_limited(empty_exam, "empty_exam"))
    (folder / "answer_sheet.pdf").write_bytes(await _read_limited(answer_sheet, "answer_sheet"))

    job = store.create()
    _create_background_task(_run_grade_job(job.id, folder, prompt or None))
    return {"id": job.id}
