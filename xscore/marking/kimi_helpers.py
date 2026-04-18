"""Shared Kimi multimodal helpers for marking (JPEG, retries, JSON recovery)."""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Protocol, runtime_checkable

from pathlib import Path

from xscore.config import apply_model_extras, resolve_pipeline_ai_model_id
from xscore.extraction.images import to_jpeg_bytes
from xscore.shared.prompt_logger import save_prompt
from xscore.shared.terminal_ui import api_latency_line, log_ai_response_debug, warn_line

# Default: JSON object mode. Pass ``response_format=None`` to omit (non-JSON prompts).
_USE_DEFAULT_JSON_OBJECT = object()


@runtime_checkable
class KimiChatClient(Protocol):
    """OpenAI-compatible client used by marking Kimi helpers (``client.chat.completions.create``)."""

    chat: Any


def page_to_jpeg_b64(image: Any, quality: int = 85) -> str:
    """Encode a PIL image as base64 JPEG (quality matches prior marking modules)."""
    return base64.b64encode(to_jpeg_bytes(image, quality=quality)).decode("utf-8")


def kimi_image_call(
    client: KimiChatClient,
    image_b64: str,
    prompt: str,
    *,
    max_tokens: int = 128,
    response_format: Any = _USE_DEFAULT_JSON_OBJECT,
    model_id: str | None = None,
    prompt_save_path: Path | None = None,
) -> str:
    """Kimi vision call with retries. Uses :func:`resolve_pipeline_ai_model_id`.

    Pass *model_id* to override the global ``PIPELINE_AI_MODEL`` for this call
    (used by name-detection to honour ``NAME_DETECTION_MODEL`` independently).

    Retries use ``2**attempt`` seconds between attempts (2s, then 4s). This differs from
    extraction's ``RETRY_BACKOFF_S`` (default 1s, configurable) — intentional; do not
    unify without checking both code paths.
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

    for attempt in range(1, 4):
        try:
            _t0 = time.perf_counter()
            resp = client.chat.completions.create(**create_kwargs)
            api_latency_line(time.perf_counter() - _t0)
            raw = resp.choices[0].message.content or ""
            log_ai_response_debug("kimi_image", model, raw)
            return raw
        except Exception as exc:
            warn_line(f"API error (attempt {attempt}/3): {exc}")
            if attempt < 3:
                time.sleep(2**attempt)
    return ""


def kimi_text_call(
    client: KimiChatClient,
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    response_format: Any = _USE_DEFAULT_JSON_OBJECT,
    thinking: bool = False,
    warn_prefix: str = "API error",
    prompt_save_path: Path | None = None,
) -> str:
    """Text-only Kimi chat with the same retry/backoff as :func:`kimi_image_call`."""
    model = resolve_pipeline_ai_model_id()
    kwargs: dict[str, Any] = dict(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
    )
    apply_model_extras(model, kwargs, thinking=thinking)
    if response_format is _USE_DEFAULT_JSON_OBJECT:
        kwargs["response_format"] = {"type": "json_object"}
    elif response_format is not None:
        kwargs["response_format"] = response_format
    if not model.startswith("kimi-k2"):
        kwargs["temperature"] = 0

    save_prompt(prompt_save_path, model=model, messages=messages)

    for attempt in range(1, 4):
        try:
            _t0 = time.perf_counter()
            response = client.chat.completions.create(**kwargs)
            api_latency_line(time.perf_counter() - _t0)
            raw = response.choices[0].message.content or ""
            log_ai_response_debug("kimi_text", model, raw)
            return raw
        except Exception as exc:
            warn_line(f"{warn_prefix} (attempt {attempt}/3): {exc}")
            if attempt < 3:
                time.sleep(2**attempt)
    return ""


def parse_json_safe(raw: str) -> dict | None:
    """Parse JSON from model text; slice object bounds; light truncation repair.

    Returns the parsed dict on success (including an empty ``{}`` if the model
    genuinely returned one), or ``None`` if the text could not be parsed as a
    JSON object at all.  Callers should check ``if result is not None`` rather
    than ``if result`` to avoid treating a valid empty dict as a parse failure.
    """
    text = raw.strip()
    if not text:
        return None

    def _as_dict(obj: Any) -> dict | None:
        return obj if isinstance(obj, dict) else None

    try:
        result = _as_dict(json.loads(text))
        if result is not None:
            return result
    except json.JSONDecodeError:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            result = _as_dict(json.loads(text[start : end + 1]))
            if result is not None:
                return result
        except json.JSONDecodeError:
            pass

    try:
        fixed = text
        if fixed.count('"') % 2 == 1:
            fixed = fixed.rstrip() + '"}'
        if not fixed.rstrip().endswith("}"):
            fixed = fixed.rstrip() + "}"
        result = _as_dict(json.loads(fixed))
        if result is not None:
            return result
    except json.JSONDecodeError:
        pass

    try:
        cleaned = re.sub(r'[\x00-\x1f]', lambda m: '\\u{:04x}'.format(ord(m.group())), text)
        result = _as_dict(json.loads(cleaned))
        if result is not None:
            return result
    except json.JSONDecodeError:
        pass

    return None
