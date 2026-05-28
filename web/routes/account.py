# -*- coding: utf-8 -*-
"""User-account routes: live check, merged login/create, logout.

Three endpoints, all under ``/api/account/*``:

  - ``POST /check``  — live availability lookup, called as the user types in
    the modal. Returns ``{"exists": bool}`` only — never the canonical display
    name (no incremental enumeration of how a name is stored).
  - ``POST /auth``   — merged login + create. If the username exists, verify
    the password; otherwise create the row. Sets the ``esg_user_account``
    cookie, attaches any prior anonymous open-mode activity in this browser to
    the account, and returns ``{ok, username, created}``.
  - ``POST /logout`` — clears the cookie.

DB helpers (``get_by_key`` / ``get_by_id`` / ``create`` / ``touch_last_login``
/ ``link_open_session``) live alongside the routes — they aren't used anywhere
else yet and have no reason to migrate out of this module until they are.
``web.user_auth.current_user_for_request`` imports ``get_by_id`` from here
lazily to avoid a circular dependency with ``web.template_ctx``.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import sqlite3
import time
from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from eXam import open_mode as _open_mode
from eXam.db import connect as _db_connect
from eXam.users import hash_password, normalize_username_key, verify_password

from ..analytics import track_request_event
from ..user_auth import (
    apply_cookie,
    clear_cookie,
    current_user_id,
    validate_password,
    validate_signup_password,
    validate_username,
)

router = APIRouter()

_NO_CACHE_HEADERS = {"Cache-Control": "no-store, no-cache", "Pragma": "no-cache"}


# ---------------------------------------------------------------------------
# Pydantic bodies
# ---------------------------------------------------------------------------

class _AuthBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=256)
    # Optional role hint. Only consulted on the create path; ignored on login
    # (DB row's stored role is authoritative). Allowed values are restricted
    # to {"student", "teacher"} below — "admin" is bootstrap-only via env.
    role: str | None = Field(default=None, max_length=32)


class _CheckBody(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)


class _ChangeUsernameBody(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)
    new_username: str = Field(..., min_length=1, max_length=128)


class _ChangePasswordBody(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=1, max_length=256)


_ALLOWED_SIGNUP_ROLES: Final[frozenset[str]] = frozenset({"student", "teacher"})


# ---------------------------------------------------------------------------
# Rate limits — two isolated dicts so a typing storm on /check can't lock out
# /auth, and vice versa. Same shape as auth_gate.enforce_login_rate_limit.
# ---------------------------------------------------------------------------

_AUTH_MAX_PER_MIN: Final[int] = 12
_CHECK_MAX_PER_MIN: Final[int] = 60
_WINDOW_S: Final[float] = 60.0

_auth_attempts: dict[str, list[float]] = {}
_auth_lock = asyncio.Lock()
_check_attempts: dict[str, list[float]] = {}
_check_lock = asyncio.Lock()


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",", 1)[0].strip()
    return request.client.host if request.client else ""


async def _enforce(bucket: dict[str, list[float]], lock: asyncio.Lock, ip: str, cap: int) -> None:
    key = (ip or "unknown").replace(":", "_")[:128]
    now = time.time()
    async with lock:
        lst = [t for t in bucket.get(key, []) if now - t < _WINDOW_S]
        if len(lst) >= cap:
            raise HTTPException(
                status_code=429,
                detail="account.err.rate_limit",
            )
        lst.append(now)
        bucket[key] = lst


async def enforce_auth_rate_limit(ip: str) -> None:
    await _enforce(_auth_attempts, _auth_lock, ip, _AUTH_MAX_PER_MIN)


async def enforce_check_rate_limit(ip: str) -> None:
    await _enforce(_check_attempts, _check_lock, ip, _CHECK_MAX_PER_MIN)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat()


def get_by_key(key: str) -> dict | None:
    with _db_connect() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role FROM users WHERE username_key = ?",
            (key,),
        ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"],
            "password_hash": row["password_hash"], "role": row["role"]}


def get_by_id(uid: int) -> dict | None:
    with _db_connect() as conn:
        row = conn.execute(
            "SELECT id, username, role FROM users WHERE id = ?",
            (uid,),
        ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def get_full_by_id(uid: int) -> dict | None:
    """Returns full user row including ``password_hash`` and timestamps.

    Used by the dashboard (created_at, last_login_at) and the change-password /
    change-username endpoints (password_hash for current-password verification).
    """
    with _db_connect() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, created_at, last_login_at "
            "FROM users WHERE id = ?",
            (uid,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "username": row["username"],
        "password_hash": row["password_hash"],
        "role": row["role"],
        "created_at": row["created_at"],
        "last_login_at": row["last_login_at"],
    }


def create(username: str, key: str, password_hash: str, *, role: str = "student") -> int | None:
    """Insert a new user row. Returns the new id, or ``None`` on UNIQUE race."""
    try:
        with _db_connect() as conn:
            cur = conn.execute(
                "INSERT INTO users (username, username_key, password_hash, role, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (username, key, password_hash, role, _now_iso()),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def touch_last_login(uid: int) -> None:
    with _db_connect() as conn:
        conn.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (_now_iso(), uid))


def link_open_session(uid: int, sid: str) -> None:
    """Attach any prior anonymous open-mode activity for this session to *uid*.

    Three UPDATEs in one connection. ``IS NULL`` guards make re-login a safe
    no-op and prevent ever overwriting another user's already-linked rows
    (e.g. shared computer with two accounts that practiced in the same
    session). Silent on any error — never breaks the auth flow.
    """
    if not sid:
        return
    try:
        with _db_connect() as conn:
            conn.execute(
                "UPDATE open_sessions SET user_id = ? WHERE id = ? AND user_id IS NULL",
                (uid, sid),
            )
            conn.execute(
                "UPDATE open_views SET user_id = ? WHERE session_id = ? AND user_id IS NULL",
                (uid, sid),
            )
            conn.execute(
                "UPDATE open_attempts SET user_id = ? WHERE session_id = ? AND user_id IS NULL",
                (uid, sid),
            )
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/api/account/check")
async def account_check(request: Request, body: _CheckBody) -> JSONResponse:
    """Live availability lookup. Never returns the stored display name."""
    await enforce_check_rate_limit(_client_ip(request))
    display, _err = validate_username(body.username)
    if display is None:
        # Don't surface validator errors here — /auth does on submit. Stay quiet.
        return JSONResponse({"exists": False}, headers=_NO_CACHE_HEADERS)
    key = normalize_username_key(display)
    row = get_by_key(key)
    return JSONResponse({"exists": row is not None}, headers=_NO_CACHE_HEADERS)


@router.post("/api/account/auth")
async def account_auth(request: Request, body: _AuthBody) -> JSONResponse:
    """Merged login + create. Sets ``esg_user_account`` cookie on success."""
    await enforce_auth_rate_limit(_client_ip(request))

    display, err = validate_username(body.username)
    if display is None:
        raise HTTPException(status_code=400, detail=err)
    err = validate_password(body.password)
    if err is not None:
        raise HTTPException(status_code=400, detail=err)

    key = normalize_username_key(display)
    row = get_by_key(key)
    sid = (request.cookies.get(_open_mode.COOKIE_NAME, "") or "").strip()

    if row is not None:
        # Login path.
        if not verify_password(body.password, row["password_hash"]):
            track_request_event(
                request, "account_auth",
                status="fail", properties={"reason": "bad_password"},
            )
            raise HTTPException(status_code=401, detail="account.err.bad_password")
        uid = row["id"]
        touch_last_login(uid)
        link_open_session(uid, sid)
        response = JSONResponse(
            {"ok": True, "username": row["username"], "created": False},
            headers=_NO_CACHE_HEADERS,
        )
        apply_cookie(response, request, uid)
        track_request_event(
            request, "account_auth",
            status="ok", properties={"created": False},
        )
        return response

    # Create path.
    # Stricter password rules apply only at signup — existing weaker accounts
    # can still log in via the looser validate_password() above.
    err = validate_signup_password(body.password)
    if err is not None:
        raise HTTPException(status_code=400, detail=err)
    role = "student"
    if body.role is not None:
        if body.role not in _ALLOWED_SIGNUP_ROLES:
            raise HTTPException(status_code=400, detail="account.err.role_invalid")
        role = body.role
    new_id = create(display, key, hash_password(body.password), role=role)
    if new_id is None:
        # Rare UNIQUE race — another concurrent request created the row.
        raise HTTPException(status_code=409, detail="account.err.taken")
    link_open_session(new_id, sid)
    response = JSONResponse(
        {"ok": True, "username": display, "created": True},
        headers=_NO_CACHE_HEADERS,
    )
    apply_cookie(response, request, new_id)
    track_request_event(
        request, "account_auth",
        status="ok", properties={"created": True, "role": role},
    )
    return response


@router.post("/api/account/logout")
async def account_logout(request: Request) -> JSONResponse:
    response = JSONResponse({"ok": True}, headers=_NO_CACHE_HEADERS)
    clear_cookie(response)
    return response


def _require_current_user(request: Request) -> dict:
    """Helper for the dashboard settings endpoints. Raises 401 if no valid cookie."""
    uid = current_user_id(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="account.err.not_logged_in")
    row = get_full_by_id(uid)
    if row is None:
        raise HTTPException(status_code=401, detail="account.err.not_logged_in")
    return row


@router.post("/api/account/change-username")
async def account_change_username(
    request: Request, body: _ChangeUsernameBody,
) -> JSONResponse:
    """Change the current user's display name. Requires the current password."""
    await enforce_auth_rate_limit(_client_ip(request))
    user = _require_current_user(request)

    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="account.err.bad_password")

    display, err = validate_username(body.new_username)
    if display is None:
        raise HTTPException(status_code=400, detail=err)
    new_key = normalize_username_key(display)

    if new_key == normalize_username_key(user["username"]):
        # No-op rename — still return ok so the UI flashes success.
        return JSONResponse(
            {"ok": True, "username": display, "changed": False},
            headers=_NO_CACHE_HEADERS,
        )

    try:
        with _db_connect() as conn:
            conn.execute(
                "UPDATE users SET username = ?, username_key = ? WHERE id = ?",
                (display, new_key, user["id"]),
            )
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="account.err.taken") from None

    track_request_event(
        request, "account_change_username", status="ok",
    )
    return JSONResponse(
        {"ok": True, "username": display, "changed": True},
        headers=_NO_CACHE_HEADERS,
    )


@router.post("/api/account/change-password")
async def account_change_password(
    request: Request, body: _ChangePasswordBody,
) -> JSONResponse:
    """Change the current user's password. Requires the current password.

    Re-issues the current browser's cookie so this session stays signed in.
    Cookies issued for other devices remain valid until they expire — full
    multi-device logout would require a cookie format change (out of scope).
    """
    await enforce_auth_rate_limit(_client_ip(request))
    user = _require_current_user(request)

    if not verify_password(body.current_password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="account.err.bad_password")

    err = validate_signup_password(body.new_password)
    if err is not None:
        raise HTTPException(status_code=400, detail=err)

    new_hash = hash_password(body.new_password)
    with _db_connect() as conn:
        conn.execute(
            "UPDATE users SET password_hash = ? WHERE id = ?",
            (new_hash, user["id"]),
        )

    response = JSONResponse({"ok": True}, headers=_NO_CACHE_HEADERS)
    apply_cookie(response, request, user["id"])
    track_request_event(request, "account_change_password", status="ok")
    return response
