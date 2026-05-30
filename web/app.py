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

import http
import logging
import mimetypes
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import starlette.middleware.gzip as _gzip_mod
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .analytics import AnalyticsMiddleware, init_db as init_analytics_db
from .auth_gate import (
    parse_ask_login_mode,
    parse_login_disabled,
    request_is_authenticated,
)
from .handouts_collect import HANDOUTS_ROOT
from .i18n import detect_language, translate
from .routes.account import router as account_router
from .routes.admin_stats import router as admin_stats_router
from .routes.code import router as code_router
from .routes.code_run import router as code_run_router
from .routes.dashboard import router as dashboard_router
from .routes.eXam_open import router as eXam_open_router
from .routes.eXam_student import router as eXam_student_router
from .routes.eXam_teacher import router as eXam_teacher_router
from .routes.grade_jobs import router as grade_jobs_router
from .routes.learn import router as learn_router
from .routes.nl_jobs import router as nl_jobs_router
from .routes.site import router as site_router
from .service import list_library_pdfs
from .template_ctx import template_ctx
from .templating import TEMPLATES

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


def _warn_if_no_app_secret_key() -> None:
    """Warn loudly (do NOT hard-fail) if site login is enabled but ``APP_SECRET_KEY``
    is unset: the cookie signing key then falls back to a public dev constant
    (``web/auth_gate.py``), making the access cookie forgeable. We warn rather than
    refuse to start so a deploy can't brick the live site when the key isn't set yet —
    but it SHOULD be set in the server ``.env``. (The Java run endpoint stays protected
    by ``JAVA_RUNNER_TOKEN`` regardless of this.)"""
    login_enabled = os.environ.get("DISABLE_LOGIN", "true").strip().lower() in ("0", "false", "no", "off")
    if login_enabled and not os.environ.get("APP_SECRET_KEY", "").strip():
        print(
            "[startup] WARNING: APP_SECRET_KEY is unset while site login is enabled — the auth "
            "cookie signing key falls back to a public dev constant (forgeable cookie). Set a "
            "high-entropy APP_SECRET_KEY in the server .env.",
            file=sys.stderr, flush=True,
        )


_warn_if_no_app_secret_key()


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # Best-effort: a failure here must not block startup, so swallow + log.
    try:
        init_analytics_db()
    except Exception:
        import logging
        logging.exception("analytics init_db() failed at startup")
    list_library_pdfs()  # pre-warm PDF index cache so the first page load is instant
    # Warm the Learn content/render caches in the background (does not delay
    # startup) so the first visitor to each subject doesn't pay the cold render
    # on the single worker.
    try:
        import threading

        from .routes.learn import warm_caches
        threading.Thread(target=warm_caches, name="warm-learn-caches", daemon=True).start()
    except Exception:
        import logging
        logging.exception("learn cache pre-warm failed to start")
    yield


app = FastAPI(title="eXercise", lifespan=_lifespan)


def _with_isolation_headers(headers: list, *, cache_control: str | None) -> list:
    """Add cross-origin isolation headers (CORP + COEP) and, when given, an
    explicit ``Cache-Control``.

    The /code playground is cross-origin isolated (COOP+COEP). Under
    ``COEP: require-corp`` every subresource it loads — and, critically, the
    Pyodide module worker's own script — must itself be served with
    ``Cross-Origin-Embedder-Policy: require-corp`` and pass the CORP check, or
    the browser refuses to load it (the worker fails with an empty error). So we
    stamp these on the whole /static tree. They are inert on non-isolated pages.

    ``cache_control`` (when not None) replaces any upstream ``Cache-Control``;
    ETag/Last-Modified from StaticFiles are kept so short-max-age mounts can
    still revalidate with a 304. ``None`` leaves StaticFiles' default (ETag only).
    """
    drop = {b"cross-origin-resource-policy", b"cross-origin-embedder-policy"}
    if cache_control is not None:
        drop |= {b"cache-control"}
    out = [(k, v) for k, v in headers if k.lower() not in drop]
    out.append((b"cross-origin-resource-policy", b"same-origin"))
    out.append((b"cross-origin-embedder-policy", b"require-corp"))
    if cache_control is not None:
        out.append((b"cache-control", cache_control.encode("latin-1")))
    return out


class IsolatedStaticFiles(StaticFiles):
    """StaticFiles that adds cross-origin isolation headers (CORP + COEP) so the
    cross-origin-isolated /code page can load these assets and spawn the Pyodide
    module worker.

    ``cache_control`` sets the per-mount caching policy: the pinned
    ``/static/vendor`` tree is ``immutable`` (fetched once, never revalidated);
    the app ``/static`` tree is a short ``max-age`` + ETag (revalidates, never
    stale after a deploy — its JS uses relative imports / bare worker URLs that
    can't carry a ``?v=`` cache-buster, so it must NOT be immutable). ``None``
    keeps StaticFiles' ETag/Last-Modified default."""

    def __init__(self, *args, cache_control: str | None = None, **kwargs):
        self._cache_control = cache_control
        super().__init__(*args, **kwargs)

    async def __call__(self, scope, receive, send):
        async def send_wrapped(message):
            if message["type"] == "http.response.start":
                message["headers"] = _with_isolation_headers(
                    list(message.get("headers", [])), cache_control=self._cache_control
                )
            await send(message)

        await super().__call__(scope, receive, send_wrapped)


# Pyodide's WASM must be served as ``application/wasm`` so the browser uses
# ``WebAssembly.instantiateStreaming`` (faster, lower memory). Starlette's StaticFiles
# derives Content-Type from Python's ``mimetypes``, whose ``.wasm`` registration is
# inconsistent across OS/Python versions — register it explicitly before the mounts.
mimetypes.add_type("application/wasm", ".wasm")

# Immutable: pinned vendor assets (Pyodide/KaTeX/CodeMirror) never change in
# place — cache them for a year with no revalidation. A vendor_fetch version
# bump that overwrites a same-named file needs a manual cache-bust (rare).
_VENDOR_CACHE = "public, max-age=31536000, immutable"
# Short max-age + ETag: app CSS/JS reuse from cache within a session but
# revalidate quickly. Deliberately NOT immutable — relative ES-module imports
# and bare Worker() URLs can't carry the ?v= cache-buster (see static_url), so
# immutable would serve stale modules after a deploy.
_STATIC_CACHE = "public, max-age=60"

VENDOR_DIR = STATIC_DIR / "vendor"
if VENDOR_DIR.is_dir():
    # Mounted before /static so its prefix wins in Starlette's route-iteration order.
    app.mount(
        "/static/vendor",
        IsolatedStaticFiles(directory=str(VENDOR_DIR), cache_control=_VENDOR_CACHE),
        name="static-vendor",
    )
if STATIC_DIR.is_dir():
    app.mount(
        "/static",
        IsolatedStaticFiles(directory=str(STATIC_DIR), cache_control=_STATIC_CACHE),
        name="static",
    )

# Handout figures live beside the handout markdown under
# ``output/eXam/handouts/<subject>/assets/`` (tracked in git like the ``.md``). Mount the handouts
# tree read-only so the Learn page can load them as ``/handout-media/<subject>/assets/<file>``.
# IsolatedStaticFiles keeps ETag/Last-Modified caching and adds CORP same-origin; its COEP header
# is inert on the (non-isolated) Learn page.
if HANDOUTS_ROOT.is_dir():
    app.mount("/handout-media", IsolatedStaticFiles(directory=str(HANDOUTS_ROOT)), name="handout-media")

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


# ---------------------------------------------------------------------------
# Themed error pages
# ---------------------------------------------------------------------------
# FastAPI's default exception handlers return raw JSON (``{"detail": "Not
# Found"}``) which renders as black-on-white text — a jarring break from the
# space-themed UI when a *browser* hits a missing/forbidden page. These handlers
# content-negotiate: a themed HTML page for navigations (Accept: text/html),
# JSON preserved for ``/api/*`` and non-HTML clients (fetch, curl, <img>).
#
# Middleware ordering keeps analytics intact:
#   - HTTPException (404/403/401) is handled by Starlette's inner
#     ExceptionMiddleware, so AnalyticsMiddleware still sees a normal response
#     and records the status code (unchanged from before).
#   - The catch-all ``Exception`` handler is invoked by the *outer*
#     ServerErrorMiddleware, AFTER AnalyticsMiddleware's ``except`` block has
#     already recorded the error event and re-raised — so error tracking works.

_ERROR_TEMPLATES = TEMPLATES  # shared env (filters/globals in web/templating.py)
_ERROR_NO_CACHE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


def _client_wants_html(request: Request) -> bool:
    """True for browser navigations — they send ``Accept: text/html``. API
    routes and non-HTML clients (fetch with JSON Accept, curl, asset tags) keep
    the JSON response so nothing programmatic breaks."""
    if request.url.path.startswith("/api/"):
        return False
    return "text/html" in request.headers.get("accept", "")


def _error_view_strings(request: Request, status_code: int, detail: object) -> dict:
    """Localized title/body/labels for the error page, with a generic fallback
    for status codes that have no dedicated string."""
    lang = detect_language(request)

    def with_fallback(key: str, generic_key: str) -> str:
        val = translate(lang, key)
        return val if val != key else translate(lang, generic_key)

    title = with_fallback(f"error.{status_code}.title", "error.generic.title")
    body = with_fallback(f"error.{status_code}.body", "error.generic.body")

    # Surface a helpful custom detail on 4xx pages (e.g. "Lesson not found", the
    # teacher-gate hint) but hide the redundant standard reason phrase ("Not
    # Found") and never leak 5xx internals to the user.
    shown_detail = ""
    if 400 <= status_code < 500 and isinstance(detail, str):
        d = detail.strip()
        try:
            standard = http.HTTPStatus(status_code).phrase
        except ValueError:
            standard = ""
        if d and d != standard and d.lower() != body.lower():
            shown_detail = d

    return {
        "status_code": status_code,
        "error_title": title,
        "error_body": body,
        "detail": shown_detail,
        "home_label": translate(lang, "error.home"),
        "page_title": translate(lang, "title.error"),
    }


def _render_error_page(request: Request, status_code: int, detail: object) -> Response:
    """Render the themed HTML error page. Falls back to JSON if rendering itself
    fails (e.g. a DB-backed 500 where the user lookup would also throw) so the
    handler can never raise."""
    try:
        ctx = template_ctx(request, **_error_view_strings(request, status_code, detail))
        return _ERROR_TEMPLATES.TemplateResponse(
            request, "error.html", ctx,
            status_code=status_code, headers=_ERROR_NO_CACHE,
        )
    except Exception:
        logging.getLogger(__name__).exception("themed error page render failed")
        msg = detail if isinstance(detail, str) else "Error"
        return JSONResponse(status_code=status_code, content={"detail": msg})


@app.exception_handler(StarletteHTTPException)
async def _on_http_exception(request: Request, exc: StarletteHTTPException) -> Response:
    if _client_wants_html(request):
        return _render_error_page(request, exc.status_code, exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(Exception)
async def _on_unhandled_exception(request: Request, exc: Exception) -> Response:
    if _client_wants_html(request):
        return _render_error_page(request, 500, None)
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


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
        "/api/account/change-username",
        "/api/account/change-password",
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

# Scoped gzip. Starlette's GZipResponder reads DEFAULT_EXCLUDED_CONTENT_TYPES as
# a module global per request, so broadening it here (before any request) takes
# effect. Default is only ("text/event-stream",); we also exclude already-
# compressed or Range-streamed binaries — notably application/pdf, so FileResponse
# byte-range streaming keeps working (pdf.js reads library PDFs by range with no
# disableRange). image/svg+xml is intentionally NOT excluded (SVG is text → the
# 188 KB logo compresses well). Registered LAST → outermost → compresses the
# final response body for every route, including /static.
_gzip_mod.DEFAULT_EXCLUDED_CONTENT_TYPES = (
    "text/event-stream",
    "application/pdf",
    "application/zip",
    "application/wasm",
    "font/",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/x-icon",
)
app.add_middleware(_gzip_mod.GZipMiddleware, minimum_size=500)


app.include_router(site_router)
app.include_router(account_router)
app.include_router(dashboard_router)
app.include_router(nl_jobs_router)
app.include_router(grade_jobs_router)
app.include_router(eXam_student_router)
app.include_router(eXam_teacher_router)
# Open-mode public-practice routes — anonymous, no login.
# NOTE: if site login is ever enabled (DISABLE_LOGIN=false), whitelist
# /eXam/practice/* in the /api/* gate above analogously to /api/auth/login.
app.include_router(eXam_open_router)
app.include_router(learn_router)
app.include_router(code_router)
app.include_router(code_run_router)
app.include_router(admin_stats_router)
