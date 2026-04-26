"""Thin Gemini-SDK adapters: response parsing and generation-config builder.

Pure leaf module — no project-internal deps beyond ``google.genai`` types and
``xscore.config`` / ``eXercise.ai_client`` helpers.
"""

from __future__ import annotations

from google.genai import types as gai_types

from eXercise.ai_client import build_gemini_thinking_config
from xscore.config import GEMINI_MAX_OUTPUT_TOKENS


def _extract_text(resp) -> str:
    """Return resp.text, tolerating None and empty-candidates responses."""
    try:
        return resp.text or ""
    except (AttributeError, ValueError):
        return ""


def _finish_reason(resp) -> str:
    """Return a human-readable diagnostic: finish_reason + block_reason if set."""
    parts = []
    try:
        if resp.candidates:
            parts.append(f"finish_reason={resp.candidates[0].finish_reason.name}")
        pf = getattr(resp, "prompt_feedback", None)
        if pf and getattr(pf, "block_reason", None):
            parts.append(f"block_reason={pf.block_reason.name}")
    except Exception:
        pass
    return ", ".join(parts) or "unknown"


def _make_gen_config(
    thinking_tokens: int | None, system: str,
    schema: dict | None = None,
    pydantic_schema=None,
    max_tokens: int | None = None,
) -> "gai_types.GenerateContentConfig":
    cfg: dict = {"max_output_tokens": max_tokens or GEMINI_MAX_OUTPUT_TOKENS}
    if pydantic_schema is not None:
        cfg["response_mime_type"] = "application/json"
        cfg["response_schema"] = pydantic_schema
    elif schema is not None:
        cfg["response_mime_type"] = "application/json"
        cfg["response_json_schema"] = schema
    if thinking_tokens is not None:
        cfg["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
    return gai_types.GenerateContentConfig(system_instruction=system, **cfg)
