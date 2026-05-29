# -*- coding: utf-8 -*-
"""Server-side Java execution endpoint (benchmark vs the client-side CheerpJ path).

``POST /api/code/run-java`` compiles + runs student Java on the server via
``web.java_runner`` and returns the same result shape the browser worker produces,
so the lesson page can A/B client vs server with ``?runtime=server``.

Security — this runs untrusted code:
  * It lives under ``/api/`` so the ``site_access_gate`` middleware (web/app.py)
    requires the access cookie (401 otherwise). ``/code/*`` is NOT server-gated, so
    the path MUST stay ``/api/...`` — the assert below guards that.
  * In addition, a secret ``X-Java-Runner-Token`` is required. **Fail closed:** if
    ``JAVA_RUNNER_TOKEN`` is unset on the server the endpoint is 404 (disabled). This
    keeps the spike caller-restricted to the developer; the in-process runner is NOT
    a real sandbox (same uid/PID-ns as uvicorn → can read /proc, bind-mounts). A
    locked-down sandbox container is required before dropping the token for students.
"""

from __future__ import annotations

import hmac
import os
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..java_runner import run_java

router = APIRouter(prefix="/api/code", tags=["code"])
# The /api/ prefix is what makes site_access_gate enforce auth on this route.
assert router.prefix.startswith("/api/"), "run-java MUST be under /api/ to be auth-gated"

_TOKEN_HEADER = "X-Java-Runner-Token"
_MAX_TOTAL = 256 * 1024   # total source bytes (code + files + harness + stdin)


class RunJavaBody(BaseModel):
    code: str = Field(default="", max_length=_MAX_TOTAL)
    files: dict[str, str] = Field(default_factory=dict)
    stdin: str = Field(default="", max_length=_MAX_TOTAL)
    check: dict[str, Any] = Field(default_factory=dict)


def _token_gate(request: Request) -> int:
    """Return an HTTP status to reject with, or 200 if the token is OK."""
    expected = (os.environ.get("JAVA_RUNNER_TOKEN") or "").strip()
    if not expected:
        return 404  # disabled / fail-closed — never run untrusted code without a token
    got = (request.headers.get(_TOKEN_HEADER) or "").strip()
    if not got or not hmac.compare_digest(got, expected):
        return 403
    return 200


@router.post("/run-java")
async def run_java_endpoint(body: RunJavaBody, request: Request) -> JSONResponse:
    status = _token_gate(request)
    if status != 200:
        return JSONResponse(status_code=status, content={"detail": "Not found" if status == 404 else "Forbidden"})

    chk = body.check if isinstance(body.check, dict) else {}
    total = len(body.code) + len(body.stdin) + sum(len(str(v)) for v in body.files.values()) \
        + len(str(chk.get("code") or ""))
    if total > _MAX_TOTAL:
        return JSONResponse(status_code=413, content={"detail": "Source too large"})

    result = await run_java(body.code, body.files, body.stdin, chk)
    return JSONResponse(content=result)
