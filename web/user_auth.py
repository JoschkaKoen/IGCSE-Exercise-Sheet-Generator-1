# -*- coding: utf-8 -*-
"""Account auth infrastructure: signed cookie, validators, current-user lookup.

Mirrors the role :mod:`web.auth_gate` plays for the site-wide access gate, but
keyed to the ``users`` row id rather than a global access code. The cookie
format and signing key match :mod:`eXam.auth` so all three signed-cookie
systems use the same primitive; only the version prefix and payload shape
differ:

  - ``web.auth_gate``  — site gate, prefix ``b"1"``, payload ``<exp>``
  - ``eXam.auth``      — student PIN session, prefix ``b"1"`` w/ different payload
  - ``web.grade_auth`` — grade unlock, prefix ``b"g1"``
  - this module        — user account, prefix ``b"u1"``, payload ``<exp>:<uid>``

Distinct prefixes prevent a token issued for one gate from validating against
another after a future payload-shape change.

DB lookup helpers live in :mod:`web.routes.account` and are imported lazily
inside :func:`current_user_for_request` to avoid a circular import.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import sqlite3
import time
import unicodedata
from typing import Final

from fastapi import Request, Response

# ---------------------------------------------------------------------------
# Cookie / token
# ---------------------------------------------------------------------------

COOKIE_NAME: Final[str] = "esg_user_account"
_TOKEN_VERSION = b"u1"
_DEFAULT_TTL_S = 90 * 24 * 60 * 60  # 90 days, matches the long-lived site cookie


def _signing_key() -> bytes:
    raw = os.environ.get("APP_SECRET_KEY", "").strip()
    if not raw:
        raw = "dev-insecure-set-APP_SECRET_KEY"
    return hashlib.sha256(raw.encode("utf-8")).digest()


def _payload(exp: int, user_id: int) -> bytes:
    return _TOKEN_VERSION + b":" + f"{exp}:{user_id}".encode("ascii")


def sign(user_id: int, *, ttl_s: int = _DEFAULT_TTL_S) -> tuple[str, int]:
    exp = int(time.time()) + int(ttl_s)
    payload = _payload(exp, user_id)
    sig = hmac.new(_signing_key(), payload, hashlib.sha256).hexdigest()
    return payload.decode("ascii") + "." + sig, ttl_s


def verify(token: str) -> int | None:
    """Return user_id if token is valid and unexpired, else None."""
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
        _, exp_s, uid_s = payload.decode("ascii").split(":", 2)
        exp = int(exp_s)
        uid = int(uid_s)
    except (ValueError, AttributeError):
        return None
    if time.time() > exp:
        return None
    return uid


def apply_cookie(response: Response, request: Request, user_id: int) -> None:
    token, ttl_s = sign(user_id)
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


def current_user_id(request: Request) -> int | None:
    return verify(request.cookies.get(COOKIE_NAME, ""))


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
#
# Username: 2–40 chars after NFC-normalize + trim + collapse-internal-whitespace.
# Reject control characters. Chinese / accented letters are fine.
# Password: 8–128 chars, no NUL bytes (would break the hash format if any code
# path ever serialised it). 8 is OWASP minimum; 128 keeps the PBKDF2 input
# bounded.

_USERNAME_MIN = 2
_USERNAME_MAX = 40
_PASSWORD_MIN = 8
_PASSWORD_MAX = 128


def _has_control_chars(s: str) -> bool:
    for ch in s:
        cat = unicodedata.category(ch)
        # Cc = control, Cf = format, Co = private use, Cn = unassigned, Cs = surrogate.
        if cat[0] == "C":
            return True
    return False


def validate_username(raw: object) -> tuple[str | None, str | None]:
    """Return ``(display, error_key)``. On failure, ``display is None``.

    The display string is NFC-normalised, stripped, and internal whitespace
    collapsed to a single space. Caller passes this to ``normalize_username_key``
    to compute the UNIQUE lookup key.
    """
    if not isinstance(raw, str):
        return None, "account.err.username_short"
    s = unicodedata.normalize("NFC", raw).strip()
    s = re.sub(r"\s+", " ", s)
    if _has_control_chars(s):
        return None, "account.err.username_short"
    if len(s) < _USERNAME_MIN:
        return None, "account.err.username_short"
    if len(s) > _USERNAME_MAX:
        return None, "account.err.username_long"
    return s, None


def validate_password(raw: object) -> str | None:
    """Return error key on failure, ``None`` on success."""
    if not isinstance(raw, str):
        return "account.err.password_short"
    if "\x00" in raw:
        return "account.err.password_short"
    if len(raw) < _PASSWORD_MIN:
        return "account.err.password_short"
    if len(raw) > _PASSWORD_MAX:
        return "account.err.password_long"
    return None


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------

def current_user_for_request(request: Request) -> dict | None:
    """Return ``{"id", "username", "role"}`` or ``None``.

    Reads the signed cookie, then looks up the row via ``users_db.get_by_id``.
    Any ``sqlite3.Error`` is swallowed (transient DB issues must not break
    template rendering — callers see "not logged in" and the user can retry).

    Imported lazily from :mod:`web.routes.account` to keep the dependency
    direction sane: ``template_ctx`` → ``user_auth`` (this module) → routes,
    not the reverse.
    """
    uid = current_user_id(request)
    if uid is None:
        return None
    try:
        from .routes.account import get_by_id  # lazy: avoid import cycle
        return get_by_id(uid)
    except sqlite3.Error:
        return None
