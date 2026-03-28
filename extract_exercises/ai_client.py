# -*- coding: utf-8 -*-
"""Shared LLM client factory.

Reads ``AI_PROVIDER`` (default ``gemini``) and returns an OpenAI-compatible
client plus the resolved model name.

Supported providers
-------------------
gemini  (default)
    base_url : https://generativelanguage.googleapis.com/v1beta/openai/
    api_key  : GOOGLE_API_KEY
    model    : gemini-2.5-flash  (override with AI_MODEL or XAI_MODEL for compat)

xai
    base_url : https://api.x.ai/v1
    api_key  : XAI_API_KEY
    model    : grok-4-1-fast-non-reasoning  (override with AI_MODEL or XAI_MODEL)

Environment variables
---------------------
AI_PROVIDER          ``gemini`` (default) or ``xai``
AI_MODEL             Override model for the chosen provider (all calls)
AI_PRECHECK_MODEL    Override model for the precheck call only
AI_MCQ_MODEL         Override model for MCQ explanation calls only
GOOGLE_API_KEY       Required when AI_PROVIDER=gemini
XAI_API_KEY          Required when AI_PROVIDER=xai
                     (legacy: also read when AI_PROVIDER not set and XAI_API_KEY
                      is present, for backward compatibility)
"""

from __future__ import annotations

import os
from typing import Any

_PROVIDERS: dict[str, dict[str, str]] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GOOGLE_API_KEY",
        "default_model": "gemini-2.5-flash",
    },
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
        "default_model": "grok-4-1-fast-non-reasoning",
    },
}


def _resolve_provider() -> str:
    """Return the active provider name.

    Defaults to ``gemini`` unless ``AI_PROVIDER`` is set to another value.
    Legacy: if ``AI_PROVIDER`` is unset but ``XAI_API_KEY`` is set and
    ``GOOGLE_API_KEY`` is *not* set, fall back to ``xai`` for backward compat.
    """
    raw = os.environ.get("AI_PROVIDER", "").strip().lower()
    if raw in _PROVIDERS:
        return raw
    if not raw:
        # Legacy auto-detect: no explicit provider
        if os.environ.get("XAI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
            return "xai"
    return "gemini"


def make_ai_client(
    *,
    model_env: str = "AI_MODEL",
    legacy_model_env: str = "XAI_MODEL",
    default_model: str | None = None,
) -> tuple[Any, str] | None:
    """Return ``(OpenAI-compatible client, model_name)`` or ``None`` on failure.

    Parameters
    ----------
    model_env:
        Primary env var name for model override (e.g. ``"AI_MODEL"``).
    legacy_model_env:
        Fallback env var to check if *model_env* is unset (e.g. ``"XAI_MODEL"``).
    default_model:
        Override the provider default model if neither env var is set.
    """
    try:
        from openai import OpenAI
    except ImportError:
        return None

    provider = _resolve_provider()
    cfg = _PROVIDERS[provider]

    api_key = os.environ.get(cfg["api_key_env"], "").strip()
    if not api_key:
        return None

    model = (
        os.environ.get(model_env, "").strip()
        or os.environ.get(legacy_model_env, "").strip()
        or default_model
        or cfg["default_model"]
    )

    try:
        client = OpenAI(api_key=api_key, base_url=cfg["base_url"])
    except Exception:
        return None

    return client, model


def get_api_key_env_name() -> str:
    """Return the env var name for the active provider's API key."""
    return _PROVIDERS[_resolve_provider()]["api_key_env"]


def get_provider_name() -> str:
    """Return the active provider name for use in error messages."""
    return _resolve_provider()
