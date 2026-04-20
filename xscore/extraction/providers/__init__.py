"""Vision LLM providers (Gemini, Kimi) and multi-pass voting."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xscore.config import AI_MODEL

from xscore.extraction.profiles.base import ExamProfile
from xscore.extraction.providers.gemini import GeminiProvider
from xscore.extraction.providers.kimi import KimiProvider


def get_provider() -> GeminiProvider | KimiProvider:
    if AI_MODEL.startswith("kimi"):
        return KimiProvider()
    return GeminiProvider()


def call_ocr_api(
    client: Any,
    image_bytes: bytes,
    page_num: int,
    profile: ExamProfile,
    prompt_save_dir: Path | None = None,
) -> dict:
    """Single (or ensemble) extraction for one page image."""
    return get_provider().extract(
        client,
        image_bytes,
        profile.prompt,
        profile.schema,
        page_num,
        profile.answer_fields,
        prompt_save_dir=prompt_save_dir,
    )


