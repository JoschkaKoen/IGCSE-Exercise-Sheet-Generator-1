"""Shared utilities for parsing and cleaning AI response text."""

from __future__ import annotations

import json
import re
from typing import Any


def strip_code_fences(raw: str) -> str:
    """Strip ``` code fences from a response string.

    Removes one optional leading fence (``` or ```lang) and one optional
    trailing fence. The fences must be on their own around the content.
    Idempotent on already-stripped input.
    """
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[^\n]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw.strip())
    return raw


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
