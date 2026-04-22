"""Shared AI multimodal helpers for marking (JPEG, retries, JSON recovery)."""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any, Protocol, runtime_checkable

from pathlib import Path

from xscore.config import MAX_RETRIES, apply_model_extras, resolve_pipeline_ai_model_id
from xscore.extraction.images import to_jpeg_bytes
from xscore.shared.prompt_logger import save_prompt, save_response
from xscore.shared.terminal_ui import api_latency_line, log_ai_response_debug, warn_line

# Default: JSON object mode. Pass ``response_format=None`` to omit (non-JSON prompts).
_USE_DEFAULT_JSON_OBJECT = object()


@runtime_checkable
class AIChatClient(Protocol):
    """OpenAI-compatible client used by marking AI helpers (``client.chat.completions.create``)."""

    chat: Any


def page_to_jpeg_b64(image: Any, quality: int = 85) -> str:
    """Encode a PIL image as base64 JPEG (quality matches prior marking modules)."""
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

    for attempt in range(MAX_RETRIES + 1):
        try:
            _t0 = time.perf_counter()
            resp = client.chat.completions.create(**create_kwargs)
            if print_latency:
                api_latency_line(time.perf_counter() - _t0)
            raw = resp.choices[0].message.content or ""
            log_ai_response_debug("ai_image", model, raw)
            save_response(prompt_save_path, raw)
            return raw
        except Exception as exc:
            warn_line(f"API error (attempt {attempt + 1}/{MAX_RETRIES + 1}): {exc}")
            if attempt < MAX_RETRIES:
                time.sleep(2 ** (attempt + 1))
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
