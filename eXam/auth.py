"""Student-auth cookie helpers.

Same HMAC-SHA256 pattern as ``web/auth_gate.py`` but keyed to ``student_id``.
Token format: ``v1:<exp>:<student_id>.<sig>``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from typing import Final

from fastapi import Request, Response

COOKIE_NAME: Final[str] = "esg_eXam_student"
_TOKEN_VERSION = b"1"
_DEFAULT_TTL_S = 12 * 60 * 60  # 12 hours; classroom session is typically much shorter


def _signing_key() -> bytes:
    raw = os.environ.get("APP_SECRET_KEY", "").strip()
    if not raw:
        raw = "dev-insecure-set-APP_SECRET_KEY"
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _payload(exp: int, student_id: int) -> bytes:
    return _TOKEN_VERSION + b":" + f"{exp}:{student_id}".encode("ascii")


def sign(student_id: int, *, ttl_s: int = _DEFAULT_TTL_S) -> tuple[str, int]:
    exp = int(time.time()) + int(ttl_s)
    payload = _payload(exp, student_id)
    sig = hmac.new(_signing_key(), payload, hashlib.sha256).hexdigest()
    return payload.decode("ascii") + "." + sig, ttl_s


def verify(token: str) -> int | None:
    """Return student_id if token is valid and unexpired, else None."""
    if not token or len(token) > 256 or token.count(".") != 1:
        return None
    payload_s, sig = token.split(".", 1)
    if len(sig) != 64 or not re.fullmatch(r"[0-9a-fA-F]{64}", sig):
        return None
    payload = payload_s.encode("ascii", errors="ignore")
    expected = hmac.new(_signing_key(), payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig.lower()):
        return None
    if not payload.startswith(_TOKEN_VERSION + b":"):
        return None
    try:
        _, exp_s, sid_s = payload.decode("ascii").split(":", 2)
        exp = int(exp_s)
        sid = int(sid_s)
    except (ValueError, AttributeError):
        return None
    if time.time() > exp:
        return None
    return sid


def apply_cookie(response: Response, request: Request, student_id: int) -> None:
    token, ttl_s = sign(student_id)
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        secure=(proto.lower() == "https"),
        max_age=ttl_s,
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def current_student_id(request: Request) -> int | None:
    return verify(request.cookies.get(COOKIE_NAME, ""))
