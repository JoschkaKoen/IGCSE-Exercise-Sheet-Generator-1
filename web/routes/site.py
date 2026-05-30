# -*- coding: utf-8 -*-
"""Site-level routes: auth, landing pages, library browsing/downloads."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from eXercise.config import EXAM_ROOT_BY_KEY, SYLLABI_DIR
from xscore.shared.pipeline_steps import max_step_number

from ..analytics import track_request_event
from ..auth_gate import (
    EXPECTED_CODE,
    apply_auth_cookie,
    codes_equal,
    enforce_login_rate_limit,
    normalize_submitted_code,
    parse_ask_login_mode,
    parse_login_disabled,
)
from ..grade_auth import (
    EXPECTED_GRADE_CODE,
    apply_grade_cookie,
    codes_equal as grade_codes_equal,
    enforce_grade_rate_limit,
    is_grade_unlocked,
)
from ..service import invalidate_library_cache, list_library_pdfs
from ..template_ctx import template_ctx as _template_ctx
from ..templating import TEMPLATES

ALLOWED_SUBJECTS = frozenset(EXAM_ROOT_BY_KEY.keys())

_HTML_NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}

router = APIRouter()


class SiteLoginBody(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)


class GradeAuthBody(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)


class LanguageBody(BaseModel):
    lang: str = Field(..., min_length=2, max_length=5)


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


def _validate_syllabus_path(subject: str, filename: str) -> Path:
    if subject not in ALLOWED_SUBJECTS:
        raise HTTPException(status_code=404, detail="Unknown subject")
    if "/" in filename or "\\" in filename or filename.strip() != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    safe = Path(filename).name
    if safe != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    root = SYLLABI_DIR.resolve()
    # Recursive lookup: PDFs may live in syllabi/igcse/ or syllabi/a_level/.
    found = next(root.glob(f"**/{safe}"), None)
    if found is None:
        raise HTTPException(status_code=404, detail="File not found")
    path = found.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid path") from exc
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return path


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@router.post("/api/auth/login")
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
        track_request_event(
            request, "auth_attempt",
            status="fail", properties={"gate": "site"},
        )
        raise HTTPException(status_code=401, detail="Invalid code")
    track_request_event(
        request, "auth_attempt",
        status="ok", properties={"gate": "site"},
    )
    response = JSONResponse(
        {"ok": True},
        headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
    )
    apply_auth_cookie(response, request, session_style=parse_ask_login_mode(request))
    return response


@router.post("/api/language")
async def set_language(request: Request, body: LanguageBody) -> JSONResponse:
    """Persist the visitor's UI language as a year-long cookie."""
    if body.lang not in ("en", "zh"):
        raise HTTPException(status_code=400, detail="Unsupported language")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        "lang", body.lang,
        max_age=60 * 60 * 24 * 365,
        samesite="lax",
        path="/",
        secure=request.url.scheme == "https",
    )
    return resp


@router.post("/api/grade/auth")
async def grade_auth_login(request: Request, body: GradeAuthBody) -> JSONResponse:
    """Set the grade-page signed cookie after valid code (separate from site auth)."""
    xff = request.headers.get("x-forwarded-for", "")
    client_ip = xff.split(",")[0].strip() if xff else (request.client.host if request.client else "")
    await enforce_grade_rate_limit(client_ip)
    if not grade_codes_equal(body.code, EXPECTED_GRADE_CODE):
        track_request_event(
            request, "auth_attempt",
            status="fail", properties={"gate": "grade"},
        )
        raise HTTPException(status_code=401, detail="Wrong code")
    track_request_event(
        request, "auth_attempt",
        status="ok", properties={"gate": "grade"},
    )
    response = JSONResponse(
        {"ok": True},
        headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
    )
    apply_grade_cookie(response, request)
    return response


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        _template_ctx(request),
        headers=_HTML_NO_CACHE,
    )


@router.get("/grade", response_class=HTMLResponse)
async def grade_page(request: Request) -> HTMLResponse:
    return TEMPLATES.TemplateResponse(
        request,
        "grade.html",
        _template_ctx(
            request,
            grade_unlocked=is_grade_unlocked(request),
            max_step=max_step_number(),
        ),
        headers=_HTML_NO_CACHE,
    )


@router.get("/library", response_class=HTMLResponse)
async def library_page(request: Request) -> HTMLResponse:
    data = list_library_pdfs()
    # "igcse_physics" is excluded from library_json because it uses a different
    # folder layout (per-topic sub-folders) that the library template cannot
    # render yet.  data (the full dict) is still passed for server-side use.
    library_json = {k: v for k, v in data.items() if k != "igcse_physics"}
    return TEMPLATES.TemplateResponse(
        request,
        "library.html",
        _template_ctx(request, library=data, library_json=library_json),
        headers=_HTML_NO_CACHE,
    )


# ---------------------------------------------------------------------------
# Library admin / files
# ---------------------------------------------------------------------------

@router.post("/api/admin/library/refresh")
async def refresh_library_cache() -> dict[str, str]:
    """Invalidate the library PDF index cache so the next page load rescans the disk."""
    invalidate_library_cache()
    return {"status": "ok"}


@router.get("/api/library/{subject}/syllabus/{filename}")
async def library_syllabus_file(subject: str, filename: str) -> FileResponse:
    path = _validate_syllabus_path(subject, filename)
    return FileResponse(path, filename=path.name, media_type="application/pdf")


@router.get("/api/library/{subject}/{filename}")
async def library_file(subject: str, filename: str) -> FileResponse:
    path = _validate_library_path(subject, filename)
    return FileResponse(path, filename=path.name, media_type="application/pdf")
