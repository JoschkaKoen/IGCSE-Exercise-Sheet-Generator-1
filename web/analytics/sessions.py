# -*- coding: utf-8 -*-
"""Session cookie issue/parse + tiny user-agent classifier.

The session cookie is an opaque UUID; it carries no auth weight and is purely
for grouping page views into "one visitor's session". HttpOnly + SameSite=Lax;
``Secure`` flag is set only when the request comes in over https (so local
dev over http://127.0.0.1 still works).

UA classification is a deliberately tiny string-match — no `ua-parser` dep —
classifying into browser family + OS family + mobile flag for the dashboard's
session table. Anything fancier isn't worth the dependency.
"""

from __future__ import annotations

import uuid
from typing import Final

from fastapi import Request, Response

SESSION_COOKIE_NAME: Final[str] = "esg_session"
_MAX_AGE_SECS: Final[int] = 365 * 24 * 60 * 60  # 1 year


def read_or_mint_session_id(request: Request) -> tuple[str, bool]:
    """Return ``(session_id, was_minted)``. *was_minted* True iff we generated a fresh id.

    Pure read: no cookie set, no I/O. The caller is responsible for calling
    :func:`apply_session_cookie` on the outgoing response when *was_minted* is True.
    """
    existing = request.cookies.get(SESSION_COOKIE_NAME)
    if existing and _looks_like_uuid(existing):
        return (existing, False)
    return (uuid.uuid4().hex, True)


def apply_session_cookie(response: Response, request: Request, session_id: str) -> None:
    """Set the session cookie on the outgoing response."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    secure = proto.lower() == "https"
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=_MAX_AGE_SECS,
        httponly=True,
        samesite="lax",
        path="/",
        secure=secure,
    )


def _looks_like_uuid(value: str) -> bool:
    """Cheap sanity check before trusting an inbound cookie value."""
    if not value or len(value) != 32:
        return False
    try:
        int(value, 16)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Tiny UA classifier — substring-match, intentionally crude. Returns
# (browser_family, os_family, is_mobile_bool). Unknown → "other".
# ---------------------------------------------------------------------------

_BROWSER_RULES: tuple[tuple[str, str], ...] = (
    ("edg/",      "edge"),     # MS Edge identifies as Edg/
    ("opr/",      "opera"),
    ("firefox/",  "firefox"),
    ("chrome/",   "chrome"),   # must come after edg/ and opr/ (they include Chrome)
    ("safari/",   "safari"),   # must come after chrome (Chrome ships Safari/ too)
)

_OS_RULES: tuple[tuple[str, str], ...] = (
    ("iphone",          "ios"),
    ("ipad",            "ios"),
    ("android",         "android"),
    ("windows",         "windows"),
    ("mac os x",        "macos"),
    ("macintosh",       "macos"),
    ("linux",           "linux"),
)

_MOBILE_HINTS = ("iphone", "ipad", "android", "mobile")


def classify_user_agent(ua: str) -> tuple[str, str, bool]:
    """Return (browser_family, os_family, is_mobile)."""
    if not ua:
        return ("other", "other", False)
    s = ua.lower()
    browser = next((name for needle, name in _BROWSER_RULES if needle in s), "other")
    os_family = next((name for needle, name in _OS_RULES if needle in s), "other")
    is_mobile = any(h in s for h in _MOBILE_HINTS)
    return (browser, os_family, is_mobile)
