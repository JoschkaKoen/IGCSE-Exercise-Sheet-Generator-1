# -*- coding: utf-8 -*-
"""Server-side Java execution endpoint for the Code page.

``POST /api/code/run-java`` compiles + runs student Java and returns the same
result shape the browser produces, so the lesson page runs Java server-side.

Execution is delegated to the isolated ``java-sandbox`` sidecar (see
``web/java_sandbox_server.py`` + ``docker-compose.yml``) when ``JAVA_SANDBOX_URL``
is set — that container has no internet, no secrets, no bind-mounts, dropped caps,
and pid/mem limits, so untrusted code can't exfiltrate or escape. With no sandbox
URL (local dev) it falls back to running in-process (NOT sandboxed).

Auth: this lives under ``/api/`` so ``site_access_gate`` (web/app.py) requires the
site cookie. Access control on top:
  * ``JAVA_RUNNER_OPEN=1`` → open to any logged-in user (the intended state once the
    sandbox is deployed — arbitrary code is safe in the sandbox).
  * else ``JAVA_RUNNER_TOKEN`` set → require a matching ``X-Java-Runner-Token``
    header (staging / pre-sandbox spike). Fail-closed: **404 if neither is set.**
"""

from __future__ import annotations

import hmac
import os
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..java_runner import run_java

router = APIRouter(prefix="/api/code", tags=["code"])
# The /api/ prefix is what makes site_access_gate enforce auth on this route.
assert router.prefix.startswith("/api/"), "run-java MUST be under /api/ to be auth-gated"

_TOKEN_HEADER = "X-Java-Runner-Token"
_MAX_TOTAL = 256 * 1024   # total source bytes (code + files + harness + stdin)
_OPEN_VALUES = {"1", "true", "yes", "on"}
_SANDBOX_URL = os.environ.get("JAVA_SANDBOX_URL", "").strip()   # e.g. http://java-sandbox:8090/run


class RunJavaBody(BaseModel):
    code: str = Field(default="", max_length=_MAX_TOTAL)
    files: dict[str, str] = Field(default_factory=dict)
    stdin: str = Field(default="", max_length=_MAX_TOTAL)
    check: dict[str, Any] = Field(default_factory=dict)


def _gate(request: Request) -> int:
    """Return an HTTP status to reject with, or 200 if allowed."""
    if os.environ.get("JAVA_RUNNER_OPEN", "").strip().lower() in _OPEN_VALUES:
        return 200  # sandboxed → safe for any logged-in (access-code) user
    expected = (os.environ.get("JAVA_RUNNER_TOKEN") or "").strip()
    if not expected:
        return 404  # fail-closed: never run untrusted code with neither open-flag nor token
    got = (request.headers.get(_TOKEN_HEADER) or "").strip()
    if not got or not hmac.compare_digest(got, expected):
        return 403
    return 200


@router.post("/run-java")
async def run_java_endpoint(body: RunJavaBody, request: Request) -> JSONResponse:
    status = _gate(request)
    if status != 200:
        return JSONResponse(status_code=status, content={"detail": "Not found" if status == 404 else "Forbidden"})

    chk = body.check if isinstance(body.check, dict) else {}
    total = len(body.code) + len(body.stdin) + sum(len(str(v)) for v in body.files.values()) \
        + len(str(chk.get("code") or ""))
    if total > _MAX_TOTAL:
        return JSONResponse(status_code=413, content={"detail": "Source too large"})

    payload = {"code": body.code, "files": body.files, "stdin": body.stdin, "check": chk}

    if _SANDBOX_URL:
        try:
            async with httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(_SANDBOX_URL, json=payload)
            return JSONResponse(status_code=200, content=resp.json())
        except Exception as e:  # sandbox down / timeout — surface cleanly to the UI
            return JSONResponse(status_code=502, content={"detail": f"Java sandbox unavailable: {e}"})

    # Local/dev fallback: run in-process. NOT sandboxed — only for local testing.
    result = await run_java(body.code, body.files, body.stdin, chk)
    return JSONResponse(content=result)
