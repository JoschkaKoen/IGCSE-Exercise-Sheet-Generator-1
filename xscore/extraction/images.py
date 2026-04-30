"""Image crop, preprocess, JPEG encoding, and MC answer normalization."""

from __future__ import annotations

import io
from typing import Any

from PIL import Image

def to_jpeg_bytes(image: Image.Image, quality: int = 95) -> bytes:
    """Convert a PIL image to JPEG bytes."""
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def normalize_mc_answer(val: Any) -> str:
    """Coerce model output to a single ``A``/``B``/``C``/``D`` or ``?``."""
    if val is None:
        return "?"
    s = str(val).upper().strip()
    if not s or s == "?":
        return "?"
    letters = [c for c in s if c in "ABCD"]
    if not letters:
        return "?"
    if len(set(letters)) > 1:
        return "?"
    return letters[0]


def normalize_extracted_record(data: dict, answer_fields: list[str]) -> dict:
    """Normalize all MC answer fields in place (and return ``data``)."""
    for field in answer_fields:
        if field in data:
            data[field] = normalize_mc_answer(data.get(field))
    return data


def failed_extraction_record(
    last_error: Exception | str | None, answer_fields: list[str]
) -> dict:
    """Build the sentinel record returned when all extraction retries fail."""
    err = str(last_error) if last_error is not None else "unknown"
    base: dict = {
        "student_name": "EXTRACTION_ERROR",
        "student_name_confidence": "failed",
        "confidence": "failed",
        "error": err,
    }
    for f in answer_fields:
        base[f] = "?"
        base[f"{f}_confidence"] = "failed"
    return normalize_extracted_record(base, answer_fields)
