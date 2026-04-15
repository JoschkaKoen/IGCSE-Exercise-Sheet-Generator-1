# -*- coding: utf-8 -*-
"""Site access gate: optional login (DISABLE_LOGIN, default on), signed cookie, ASK_LOGIN / ?ask_login=."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import time
from typing import Final

from fastapi import HTTPException, Request, Response

ACCESS_COOKIE_NAME: Final[str] = "esg_site_auth"

# Default code; override with ACCESS_CODE env (still compared with timing-safe digest).
_DEFAULT_CODE = "NBFLS"
EXPECTED_CODE: Final[str] = os.environ.get("ACCESS_CODE", _DEFAULT_CODE).strip().upper() or _DEFAULT_CODE

_LONG_LIVED_SECS = 90 * 24 * 60 * 60
_SESSION_COOKIE_SECS = 8 * 60 * 60  # when ask_login mode: bounded lifetime, no Max-Age on cookie

_login_attempts: dict[str, list[float]] = {}
_attempts_lock = asyncio.Lock()
_MAX_ATTEMPTS_PER_MINUTE = 12
_WINDOW_SECS = 60.0

_TOKEN_VERSION = b"1"

_FALSEY = frozenset(("0", "false", "no", "off"))
_TRUTHY = frozenset(("1", "true", "yes", "on"))


def parse_login_disabled(request: Request) -> bool:
    """True = no login modal and no API cookie check.

    Env ``DISABLE_LOGIN`` defaults to ``true`` (login off). Query ``?disable_login=`` overrides
    when set: truthy values disable login; ``0``/``false``/``no``/``off`` enable the gate.
    """
    q = (request.query_params.get("disable_login") or "").strip().lower()
    if q in _FALSEY:
        return False
    if q in _TRUTHY:
        return True
    v = os.environ.get("DISABLE_LOGIN", "true").strip().lower()
    return v not in _FALSEY


def _signing_key() -> bytes:
    raw = os.environ.get("APP_SECRET_KEY", "").strip()
    if not raw:
        raw = "dev-insecure-set-APP_SECRET_KEY"
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _ask_login_env() -> bool:
    return os.environ.get("ASK_LOGIN", "").strip().lower() in ("1", "true", "yes", "on")


def parse_ask_login_mode(request: Request) -> bool:
    """True = session-style cookie; env ASK_LOGIN or query ?ask_login=1|true|yes|on."""
    if _ask_login_env():
        return True
    q = (request.query_params.get("ask_login") or "").strip().lower()
    return q in ("1", "true", "yes", "on")


def normalize_submitted_code(raw: str) -> str | None:
    """Reject non-ASCII and oversize input before comparison."""
    s = raw.strip()
    if not s or len(s) > 64:
        return None
    try:
        s.encode("ascii")
    except UnicodeEncodeError:
        return None
    if not re.fullmatch(r"[A-Za-z0-9\-_.]+", s):
        return None
    return s.upper()


def codes_equal(submitted_normalized: str, expected: str) -> bool:
    """Constant-length compare to limit timing leaks on length."""
    a = submitted_normalized.encode("utf-8")[:32].ljust(32, b"\0")
    b = expected.encode("utf-8")[:32].ljust(32, b"\0")
    return hmac.compare_digest(a, b)


def _make_payload(expires_at: float) -> bytes:
    return _TOKEN_VERSION + b":" + str(int(expires_at)).encode("ascii")


def sign_access_token(expires_at: float) -> str:
    key = _signing_key()
    payload = _make_payload(expires_at)
    sig = hmac.new(key, payload, hashlib.sha256).hexdigest()
    return payload.decode("ascii") + "." + sig


def verify_access_token(token: str) -> bool:
    if not token or len(token) > 256 or token.count(".") != 1:
        return False
    payload_s, sig = token.split(".", 1)
    if len(sig) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", sig):
        return False
    try:
        payload = payload_s.encode("ascii")
    except UnicodeEncodeError:
        return False
    key = _signing_key()
    expected_sig = hmac.new(key, payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, sig.lower()):
        return False  # hexdigest is lowercase
    if not payload.startswith(_TOKEN_VERSION + b":"):
        return False
    try:
        exp = int(payload.split(b":", 1)[1].decode("ascii"))
    except (ValueError, IndexError):
        return False
    return time.time() <= float(exp)


def read_cookie_token(request: Request) -> str | None:
    return request.cookies.get(ACCESS_COOKIE_NAME)


def request_is_authenticated(request: Request) -> bool:
    token = read_cookie_token(request)
    if not token:
        return False
    return verify_access_token(token)


async def enforce_login_rate_limit(client_ip: str) -> None:
    now = time.time()
    ip = (client_ip or "unknown").replace(":", "_")[:128]
    async with _attempts_lock:
        lst = [t for t in _login_attempts.get(ip, []) if now - t < _WINDOW_SECS]
        if len(lst) >= _MAX_ATTEMPTS_PER_MINUTE:
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Wait a minute and try again.",
            )
        lst.append(now)
        _login_attempts[ip] = lst


def apply_auth_cookie(response: Response, request: Request, *, session_style: bool) -> None:
    """Set HttpOnly signed cookie. session_style omits Max-Age (browser session) but token still expires server-side."""
    if session_style:
        exp = time.time() + _SESSION_COOKIE_SECS
        max_age = None
    else:
        exp = time.time() + _LONG_LIVED_SECS
        max_age = _LONG_LIVED_SECS
    token = sign_access_token(exp)
    # Respect X-Forwarded-Proto so the Secure flag is set correctly behind a TLS proxy.
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    secure = proto.lower() == "https"
    kwargs: dict = {
        "key": ACCESS_COOKIE_NAME,
        "value": token,
        "httponly": True,
        "samesite": "lax",
        "path": "/",
        "secure": secure,
    }
    if max_age is not None:
        kwargs["max_age"] = max_age
    response.set_cookie(**kwargs)
