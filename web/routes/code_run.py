# -*- coding: utf-8 -*-
"""Server-side compiled-language execution endpoints for the Code page.

``POST /api/code/run-java`` and ``POST /api/code/run-c`` compile + run student Java
/ C and return the same result shape the browser produces, so the lesson page runs
those languages server-side.

Execution is delegated to the isolated sandbox sidecar (see
``web/java_sandbox_server.py`` + ``docker-compose.yml``) when the matching
``*_SANDBOX_URL`` is set — that container has no internet, no secrets, no
bind-mounts, dropped caps, and pid/mem limits, so untrusted code can't exfiltrate or
escape. With no sandbox URL (local dev) it falls back to running in-process (NOT
sandboxed). Both languages share one sidecar container (different paths).

Auth: these live under ``/api/`` so ``site_access_gate`` (web/app.py) requires the
site cookie. Per-language access control on top (so C and Java roll out
independently):
  * ``{JAVA,C}_RUNNER_OPEN=1`` → open to any logged-in user (the intended state once
    the sandbox is deployed — arbitrary code is safe in the sandbox).
  * else ``{JAVA,C}_RUNNER_TOKEN`` set → require a matching
    ``X-{Java,C}-Runner-Token`` header (staging / pre-sandbox spike).
    Fail-closed: **404 if neither is set.**
"""

from __future__ import annotations

import hmac
import os
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..c_runner import run_c
from ..java_runner import run_java

router = APIRouter(prefix="/api/code", tags=["code"])
# The /api/ prefix is what makes site_access_gate enforce auth on these routes.
assert router.prefix.startswith("/api/"), "run-java/run-c MUST be under /api/ to be auth-gated"

_MAX_TOTAL = 256 * 1024   # total source bytes (code + files + harness + stdin)
_OPEN_VALUES = {"1", "true", "yes", "on"}
# Per-language sidecar URLs (same container, different path) — e.g.
# http://java-sandbox:8090/run and .../run-c. Empty → in-process dev fallback.
_JAVA_SANDBOX_URL = os.environ.get("JAVA_SANDBOX_URL", "").strip()
_C_SANDBOX_URL = os.environ.get("C_SANDBOX_URL", "").strip()


class RunBody(BaseModel):
    code: str = Field(default="", max_length=_MAX_TOTAL)
    files: dict[str, str] = Field(default_factory=dict)
    stdin: str = Field(default="", max_length=_MAX_TOTAL)
    check: dict[str, Any] = Field(default_factory=dict)


def _gate(request: Request, *, open_var: str, token_var: str, token_header: str) -> int:
    """Return an HTTP status to reject with, or 200 if allowed. Parameterized over
    the per-language env vars so Java and C gate independently."""
    if os.environ.get(open_var, "").strip().lower() in _OPEN_VALUES:
        return 200  # sandboxed → safe for any logged-in (access-code) user
    expected = (os.environ.get(token_var) or "").strip()
    if not expected:
        return 404  # fail-closed: never run untrusted code with neither open-flag nor token
    got = (request.headers.get(token_header) or "").strip()
    if not got or not hmac.compare_digest(got, expected):
        return 403
    return 200


def _too_large(body: RunBody) -> bool:
    """True if the total source (code + stdin + files + harness code) exceeds the cap."""
    chk = body.check if isinstance(body.check, dict) else {}
    total = len(body.code) + len(body.stdin) + sum(len(str(v)) for v in body.files.values()) \
        + len(str(chk.get("code") or ""))
    return total > _MAX_TOTAL


async def _dispatch(body: RunBody, sandbox_url: str, runner, lang: str) -> JSONResponse:
    """Shared size-check → sandbox-forward → in-process-fallback for both languages."""
    if _too_large(body):
        return JSONResponse(status_code=413, content={"detail": "Source too large"})
    chk = body.check if isinstance(body.check, dict) else {}
    payload = {"code": body.code, "files": body.files, "stdin": body.stdin, "check": chk}

    if sandbox_url:
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(sandbox_url, json=payload)
            return JSONResponse(status_code=200, content=resp.json())
        except Exception as e:  # sandbox down / timeout — surface cleanly to the UI
            return JSONResponse(status_code=502, content={"detail": f"{lang} sandbox unavailable: {e}"})

    # Local/dev fallback: run in-process. NOT sandboxed — only for local testing.
    result = await runner(body.code, body.files, body.stdin, chk)
    return JSONResponse(content=result)


@router.post("/run-java")
async def run_java_endpoint(body: RunBody, request: Request) -> JSONResponse:
    status = _gate(request, open_var="JAVA_RUNNER_OPEN", token_var="JAVA_RUNNER_TOKEN",
                   token_header="X-Java-Runner-Token")
    if status != 200:
        return JSONResponse(status_code=status, content={"detail": "Not found" if status == 404 else "Forbidden"})
    return await _dispatch(body, _JAVA_SANDBOX_URL, run_java, "Java")


@router.post("/run-c")
async def run_c_endpoint(body: RunBody, request: Request) -> JSONResponse:
    status = _gate(request, open_var="C_RUNNER_OPEN", token_var="C_RUNNER_TOKEN",
                   token_header="X-C-Runner-Token")
    if status != 200:
        return JSONResponse(status_code=status, content={"detail": "Not found" if status == 404 else "Forbidden"})
    return await _dispatch(body, _C_SANDBOX_URL, run_c, "C")
