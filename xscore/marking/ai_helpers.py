"""Shared AI multimodal helpers for marking (JPEG, retries, JSON recovery)."""

from __future__ import annotations

import base64
import time
from typing import Any, Protocol, runtime_checkable

from pathlib import Path

from eXercise.ai_client import is_503_error
from xscore.config import NAME_JPEG_QUALITY, apply_model_extras, resolve_pipeline_ai_model_id
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
    model_id: str | None = None,
    prompt_save_path: Path | None = None,
    print_latency: bool = True,
) -> str:
    """Vision call with retries. Uses :func:`resolve_pipeline_ai_model_id`.

    Pass *model_id* to override the global ``PIPELINE_AI_MODEL`` for this call
    (used by name-detection to honour ``NAME_DETECTION_MODEL`` independently).

    Retries once on 503 after 0.1 s; all other errors fail immediately.
    """
    model = model_id if model_id is not None else resolve_pipeline_ai_model_id()
    create_kwargs: dict[str, Any] = dict(
        model=model,
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
    apply_model_extras(model, create_kwargs, thinking=False)
    if response_format is _USE_DEFAULT_JSON_OBJECT:
        create_kwargs["response_format"] = {"type": "json_object"}
    elif response_format is not None:
        create_kwargs["response_format"] = response_format

    save_prompt(prompt_save_path, model=model, messages=create_kwargs["messages"])

    for attempt in range(2):  # initial attempt + 1 retry on 503
        try:
            _t0 = time.perf_counter()
            resp = client.chat.completions.create(**create_kwargs)
            if print_latency:
                api_latency_line(time.perf_counter() - _t0)
            raw = resp.choices[0].message.content or ""
            if not raw:
                warn_line(f"[{model}] returned empty content — check thinking/token budget")
            log_ai_response_debug("ai_image", model, raw)
            save_response(prompt_save_path, raw)
            return raw
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            warn_line(f"API error (attempt {attempt + 1}/2): {exc}")
            if attempt == 0 and is_503_error(exc):
                time.sleep(0.1)
            else:
                break
    return ""


