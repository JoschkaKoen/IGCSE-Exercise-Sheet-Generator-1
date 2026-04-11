# -*- coding: utf-8 -*-
"""Shared LLM client factory.

Provider is inferred automatically from the model name — no separate
AI_PROVIDER setting is needed.

Supported providers (auto-detected by model name prefix)
---------------------------------------------------------
gemini  (model names starting with ``gemini``)
    base_url : https://generativelanguage.googleapis.com/v1beta/openai/
    api_key  : GOOGLE_API_KEY
    example  : gemini-2.5-flash, gemini-2.0-flash

xai  (model names starting with ``grok``)
    base_url : https://api.x.ai/v1
    api_key  : XAI_API_KEY
    example  : grok-4-1-fast-non-reasoning, grok-3

qwen  (model names starting with ``qwen``)
    base_url : https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key  : DASHSCOPE_API_KEY
    example  : qwen3.6-plus, qwen3-32b
    note     : Uses streaming with enable_thinking=True; call sites must use
               collect_streamed_response() instead of reading message.content.

Per-call-type model overrides
------------------------------
Set a model name and the provider is picked automatically:

    AI_MODEL     global default model for all calls (default: gemini-2.5-flash)
    NL_MODEL     prompt interpretation  (overrides AI_MODEL for this call)
    MCQ_MODEL    AI explanation generation (overrides AI_MODEL for this call)

Environment variables (API keys)
---------------------------------
GOOGLE_API_KEY    Required for gemini models
XAI_API_KEY       Required for grok models
DASHSCOPE_API_KEY Required for qwen models
"""

from __future__ import annotations

import os
from typing import Any

_PROVIDERS: dict[str, dict[str, str]] = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key_env": "GOOGLE_API_KEY",
    },
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "api_key_env": "XAI_API_KEY",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
    },
}

# Model name prefix → provider.  Checked in order; first match wins.
_MODEL_PREFIXES: list[tuple[str, str]] = [
    ("gemini", "gemini"),
    ("grok",   "xai"),
    ("qwen",   "qwen"),
]

# Providers that use streaming with a reasoning/thinking phase.  Call sites
# must use collect_streamed_response() instead of reading message.content.
_THINKING_PROVIDERS: frozenset[str] = frozenset({"qwen"})

# Fallback model when no model env var is set anywhere.
_DEFAULT_MODEL = "gemini-2.5-flash"


def provider_for_model(model: str) -> str:
    """Return the provider name for *model* based on its name prefix.

    Falls back to ``gemini`` for unknown model names.
    """
    m = model.lower()
    for prefix, provider in _MODEL_PREFIXES:
        if m.startswith(prefix):
            return provider
    return "gemini"


def make_ai_client(
    *,
    model_env: str = "AI_MODEL",
    legacy_model_env: str = "XAI_MODEL",
    default_model: str | None = None,
) -> tuple[Any, str, str] | None:
    """Return ``(client, model_name, provider)`` or ``None`` if the API key is missing.

    Parameters
    ----------
    model_env:
        Primary env var for model override (e.g. ``"NL_MODEL"``).
    legacy_model_env:
        Fallback env var if *model_env* is unset (e.g. ``"AI_MODEL"``).
    default_model:
        Model to use when neither env var is set.  Defaults to
        ``AI_MODEL`` → ``XAI_MODEL`` → ``_DEFAULT_MODEL``.
    """
    try:
        from openai import OpenAI
    except ImportError:
        return None

    model = (
        os.environ.get(model_env, "").strip()
        or os.environ.get(legacy_model_env, "").strip()
        or default_model
        or os.environ.get("AI_MODEL", "").strip()
        or _DEFAULT_MODEL
    )

    provider = provider_for_model(model)
    cfg = _PROVIDERS[provider]

    api_key = os.environ.get(cfg["api_key_env"], "").strip()
    if not api_key:
        return None

    try:
        client = OpenAI(api_key=api_key, base_url=cfg["base_url"])
    except Exception:
        return None

    return client, model, provider


def strip_json_fences(raw: str) -> str:
    """Remove markdown code fences that some models add despite being told not to.

    Handles ```json ... ```, ``` ... ```, and leading/trailing whitespace.
    Falls back to extracting the outermost { ... } block when prose surrounds the JSON.
    """
    import re
    s = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)```\s*$", s)
    if fence:
        return fence.group(1).strip()
    m = re.search(r"\{[\s\S]*\}", s)
    if m:
        return m.group(0)
    return s


def get_api_key_env_name(provider: str | None = None) -> str:
    """Return the env var name for the given provider's API key.

    If *provider* is None, returns the key env for the default model's provider.
    """
    p = provider if provider else provider_for_model(
        os.environ.get("AI_MODEL", "").strip() or _DEFAULT_MODEL
    )
    return _PROVIDERS[p]["api_key_env"]


def is_thinking_provider(provider: str) -> bool:
    """Return True when *provider* requires streaming + enable_thinking.

    Call sites should use ``collect_streamed_response()`` instead of reading
    ``completion.choices[0].message.content`` when this returns True.
    """
    return provider in _THINKING_PROVIDERS


def collect_streamed_response(stream: Any) -> str:
    """Consume a streaming chat completion and return the answer text.

    Skips ``delta.reasoning_content`` (the thinking/scratchpad) and
    accumulates only ``delta.content`` (the final answer).  Works for any
    provider that returns a streaming completion, but is specifically
    designed for Qwen's thinking-mode responses.
    """
    parts: list[str] = []
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            parts.append(delta.content)
    return "".join(parts).strip()
