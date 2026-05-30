# -*- coding: utf-8 -*-
"""Isolated Java execution sidecar — a thin HTTP wrapper around ``web.java_runner``.

Deployed as the ``java-sandbox`` service (see ``docker-compose.yml``) in a
locked-down container: **no internet** (an ``internal`` compose network), a
**read-only root FS** with only a small ``tmpfs`` workdir, **all Linux caps
dropped**, ``no-new-privileges``, ``pids``/``mem`` limits, a **non-root user**,
**no bind-mounts**, and — critically — **no secrets in its environment** (no
``env_file``). So even if hostile student code reads ``/proc/1/environ`` or the
filesystem, there are no API keys, no ``APP_SECRET_KEY``, and no exam/output data
to find, and it cannot reach the network.

The public endpoint ``web/routes/code_run.py`` (auth-gated, under ``/api/``)
forwards student code here over the internal network. **This service has no auth
of its own and must never be given an external port** — it is reachable only from
the web container on the private network.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from .c_runner import run_c
from .java_runner import run_java

# Serves both Java (/run) and C (/run-c) — same locked-down container, same image.
app = FastAPI(title="code-sandbox", docs_url=None, redoc_url=None, openapi_url=None)


class RunBody(BaseModel):
    code: str = ""
    files: dict[str, str] = Field(default_factory=dict)
    stdin: str = ""
    check: dict[str, Any] = Field(default_factory=dict)


@app.get("/healthz")
async def healthz() -> dict[str, bool]:
    return {"ok": True}


@app.post("/run")
async def run(body: RunBody) -> dict[str, Any]:
    return await run_java(body.code, body.files, body.stdin, body.check)


@app.post("/run-c")
async def run_c_endpoint(body: RunBody) -> dict[str, Any]:
    return await run_c(body.code, body.files, body.stdin, body.check)
