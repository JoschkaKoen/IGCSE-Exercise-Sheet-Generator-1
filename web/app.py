# -*- coding: utf-8 -*-
"""FastAPI app: landing page, job API, exam library downloads.

Top-level wiring only — app construction, static files, favicon, the auth
middleware, and router includes. Route handlers live under ``web/routes/``:

- ``site``: auth, landing pages, library
- ``nl_jobs``: natural-language exercise generation
- ``grade_jobs``: full xScore grading pipeline
"""

from __future__ import annotations

from eXercise.env_load import load_project_env

load_project_env()

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .analytics import AnalyticsMiddleware, init_db as init_analytics_db
from .auth_gate import (
    parse_ask_login_mode,
    parse_login_disabled,
    request_is_authenticated,
)
from .routes.account import router as account_router
from .routes.admin_stats import router as admin_stats_router
from .routes.eXam_open import router as eXam_open_router
from .routes.eXam_student import router as eXam_student_router
from .routes.eXam_teacher import router as eXam_teacher_router
from .routes.grade_jobs import router as grade_jobs_router
from .routes.nl_jobs import router as nl_jobs_router
from .routes.site import router as site_router
from .service import list_library_pdfs

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Best-effort: a failure here must not block startup, so swallow + log.
    try:
        init_analytics_db()
    except Exception:
        import logging
        logging.exception("analytics init_db() failed at startup")
    list_library_pdfs()  # pre-warm PDF index cache so the first page load is instant
    yield


app = FastAPI(title="eXercise", lifespan=_lifespan)


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
    if path.startswith("/api/") and path not in (
        "/api/auth/login",
        "/api/grade/auth",
        "/api/language",
        "/api/account/auth",
        "/api/account/check",
        "/api/account/logout",
    ):
        if not login_disabled and not request.state.site_auth_ok:
            return JSONResponse(
                status_code=401,
                content={"detail": "Login required"},
                headers={"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"},
            )

    return await call_next(request)


# Analytics middleware is registered *after* the gate decorator so that it
# ends up OUTERMOST in the runtime stack. Starlette's ``add_middleware``
# does ``user_middleware.insert(0, ...)``; the resulting list is then
# iterated in REVERSE inside ``build_middleware_stack`` — net effect: the
# LAST registered middleware wraps everything else. Outermost = sees every
# request, including the 401/403 responses the gate above issues.
app.add_middleware(AnalyticsMiddleware)


app.include_router(site_router)
app.include_router(account_router)
app.include_router(nl_jobs_router)
app.include_router(grade_jobs_router)
app.include_router(eXam_student_router)
app.include_router(eXam_teacher_router)
# Open-mode public-practice routes — anonymous, no login.
# NOTE: if site login is ever enabled (DISABLE_LOGIN=false), whitelist
# /eXam/practice/* in the /api/* gate above analogously to /api/auth/login.
app.include_router(eXam_open_router)
app.include_router(admin_stats_router)
