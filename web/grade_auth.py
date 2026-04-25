# -*- coding: utf-8 -*-
"""Grade page access gate: separate signed cookie + endpoint, shared signing key with auth_gate."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import time
from typing import Final

from fastapi import HTTPException, Request, Response

from .auth_gate import _signing_key

GRADE_COOKIE_NAME: Final[str] = "esg_grade_auth"

# Default code is the literal "grade"; override with GRADE_ACCESS_CODE env.
EXPECTED_GRADE_CODE: Final[str] = (os.environ.get("GRADE_ACCESS_CODE", "grade").strip() or "grade")

_LONG_LIVED_SECS = 90 * 24 * 60 * 60  # 90 days

_grade_attempts: dict[str, list[float]] = {}
_grade_attempts_lock = asyncio.Lock()
_MAX_ATTEMPTS_PER_MINUTE = 12
_WINDOW_SECS = 60.0

# "g1" namespace prevents tokens minted by the site-wide gate from validating here.
_TOKEN_VERSION = b"g1"


def _make_payload(expires_at: float) -> bytes:
    return _TOKEN_VERSION + b":" + str(int(expires_at)).encode("ascii")


def _sign(expires_at: float) -> str:
    payload = _make_payload(expires_at)
    sig = hmac.new(_signing_key(), payload, hashlib.sha256).hexdigest()
    return payload.decode("ascii") + "." + sig


def _verify(token: str) -> bool:
    if not token or len(token) > 256 or token.count(".") != 1:
        return False
    payload_s, sig = token.split(".", 1)
    if len(sig) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", sig):
        return False
    try:
        payload = payload_s.encode("ascii")
    except UnicodeEncodeError:
        return False
    expected_sig = hmac.new(_signing_key(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig.lower()):
        return False
    if not payload.startswith(_TOKEN_VERSION + b":"):
        return False
    try:
        exp = int(payload.split(b":", 1)[1].decode("ascii"))
    except (ValueError, IndexError):
        return False
    return time.time() <= float(exp)


def is_grade_unlocked(request: Request) -> bool:
    token = request.cookies.get(GRADE_COOKIE_NAME)
    if not token:
        return False
    return _verify(token)


def require_grade_unlock(request: Request) -> None:
    """FastAPI dependency: raise 401 if the grade gate isn't unlocked."""
    if not is_grade_unlocked(request):
        raise HTTPException(status_code=401, detail="Grade access required")


async def enforce_grade_rate_limit(client_ip: str) -> None:
    now = time.time()
    ip = (client_ip or "unknown").replace(":", "_")[:128]
    async with _grade_attempts_lock:
        lst = [t for t in _grade_attempts.get(ip, []) if now - t < _WINDOW_SECS]
        if len(lst) >= _MAX_ATTEMPTS_PER_MINUTE:
            raise HTTPException(
                status_code=429,
                detail="Too many attempts. Wait a minute and try again.",
            )
        lst.append(now)
        _grade_attempts[ip] = lst


def codes_equal(submitted: str, expected: str) -> bool:
    """Constant-length compare to limit timing leaks on length."""
    a = submitted.encode("utf-8")[:64].ljust(64, b"\0")
    b = expected.encode("utf-8")[:64].ljust(64, b"\0")
    return hmac.compare_digest(a, b)


def apply_grade_cookie(response: Response, request: Request) -> None:
    """Set the HttpOnly signed cookie for the grade gate (90-day long-lived).

    Respects ``X-Forwarded-Proto`` so ``Secure`` is correct behind a TLS proxy;
    omitted on plain ``http://`` so local dev works.
    """
    exp = time.time() + _LONG_LIVED_SECS
    token = _sign(exp)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    secure = proto.lower() == "https"
    response.set_cookie(
        key=GRADE_COOKIE_NAME,
        value=token,
        max_age=_LONG_LIVED_SECS,
        httponly=True,
        samesite="lax",
        path="/",
        secure=secure,
    )
