"""Shared AI multimodal helpers for marking (JPEG, retries, JSON recovery)."""

from __future__ import annotations

import base64
import time
from typing import Any, Protocol, runtime_checkable

from pathlib import Path

from eXercise.ai_client import build_thinking_kwargs
from eXercise.api_retry import retry_api_call
from xscore.config import NAME_JPEG_QUALITY
from xscore.extraction.images import to_jpeg_bytes
from xscore.shared.prompt_logger import save_prompt, save_response
from xscore.shared.response_parsing import parse_json_safe  # noqa: F401 — re-exported for callers
from xscore.shared.terminal_ui import api_latency_line, log_ai_response_debug, warn_line

# Default: JSON object mode. Pass ``response_format=None`` to omit (non-JSON prompts).
_USE_DEFAULT_JSON_OBJECT = object()


@runtime_checkable
class AIChatClient(Protocol):
    """OpenAI-compatible client used by marking AI helpers (``client.chat.completions.create``)."""

    chat: Any


def page_to_jpeg_b64(image: Any, quality: int = NAME_JPEG_QUALITY) -> str:
    """Encode a PIL image as base64 JPEG."""
    return base64.b64encode(to_jpeg_bytes(image, quality=quality)).decode("utf-8")


def ai_image_call(
    client: AIChatClient,
    image_b64: str,
    prompt: str,
    *,
    max_tokens: int = 128,
    response_format: Any = _USE_DEFAULT_JSON_OBJECT,
    model_id: str,
    provider: str | None = None,
    thinking_tokens: int | None = None,
    prompt_save_path: Path | None = None,
    print_latency: bool = True,
) -> str:
    """Non-streaming vision call with retries.

    *thinking_tokens* must be 0 (or None) for non-Grok providers — this helper
    cannot consume streaming responses. Pass non-zero thinking and the call
    raises a ``RuntimeError`` naming the env var to fix.

    Retries once on 503 after 0.1 s; all other errors fail immediately.
    """
    create_kwargs: dict[str, Any] = dict(
        model=model_id,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            }
        ],
        max_tokens=max_tokens,
    )
    if provider is not None:
        use_stream, thinking_kw = build_thinking_kwargs(provider, thinking_tokens)
        if use_stream:
            raise RuntimeError(
                f"ai_image_call cannot stream — set thinking_tokens=0 for {provider} "
                f"(got thinking_tokens={thinking_tokens!r}). "
                f"This helper is non-streaming; the caller must pick a model "
                f"line whose thinking budget is 0 (e.g. NAME_DETECTION_MODEL=qwen3.6-plus, 0, 64)."
            )
        create_kwargs.update(thinking_kw)
    if response_format is _USE_DEFAULT_JSON_OBJECT:
        create_kwargs["response_format"] = {"type": "json_object"}
    elif response_format is not None:
        create_kwargs["response_format"] = response_format

    save_prompt(prompt_save_path, model=model_id, messages=create_kwargs["messages"])

    def _do_call() -> tuple[str, str]:
        _resp = client.chat.completions.create(**create_kwargs)
        return (
            _resp.choices[0].message.content or "",
            getattr(_resp.choices[0].message, "reasoning_content", "") or "",
        )

    _t0 = time.perf_counter()
    try:
        raw, thinking_text = retry_api_call(_do_call, label=f"AI image ({model_id})")
    except Exception:
        return ""
    if print_latency:
        api_latency_line(time.perf_counter() - _t0)
    if not raw:
        warn_line(f"[{model_id}] returned empty content — check thinking/token budget")
    log_ai_response_debug("ai_image", model_id, raw)
    save_response(prompt_save_path, raw, thinking=thinking_text)
    return raw


