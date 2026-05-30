# -*- coding: utf-8 -*-
"""Shared per-request template context.

Every router that renders a Jinja template calls ``template_ctx(request, **extra)``
instead of building a raw dict. That keeps language / login / flag state in a
single source of truth and means new keys (e.g. a future banner flag) only
need to be added here.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from fastapi import Request

from .i18n import (
    STRINGS,
    detect_language,
    flag_for_destination,
    html_lang_attr,
    translate,
)
from .user_auth import current_user_for_request


@lru_cache(maxsize=8)
def _i18n_json(lang: str) -> str:
    """Per-language UI string table, dumped once (``STRINGS`` is static at import)."""
    return json.dumps(STRINGS.get(lang, STRINGS["en"]), ensure_ascii=False)


def template_ctx(request: Request, **extra: Any) -> dict[str, Any]:
    lang = detect_language(request)
    login_disabled = getattr(request.state, "login_disabled", True)
    auth_ok = getattr(request.state, "site_auth_ok", False)
    ctx: dict[str, Any] = {
        "request": request,
        "login_disabled": login_disabled,
        "needs_site_login": (not login_disabled) and (not auth_ok),
        "ask_login_mode": getattr(request.state, "ask_login_mode", False),
        "lang": lang,
        "html_lang": html_lang_attr(lang),
        "t": lambda key: translate(lang, key),
        "flag": flag_for_destination(lang),
        # Full string table dumped once per request as JSON. base.html assigns
        # it to window.i18n so any inline / module JS can look strings up by
        # key. ensure_ascii=False keeps Chinese readable in view-source.
        "i18n_json": _i18n_json(lang),
        # Per-render lookup of the logged-in user (None if no cookie / expired
        # / row missing). Only runs on template-rendering routes — JSON /api/*
        # endpoints don't go through here.
        "current_user": current_user_for_request(request),
    }
    ctx.update(extra)
    return ctx
