"""Vision LLM providers (Gemini, Kimi) and multi-pass voting."""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

from google import genai

from xscore.config import AI_MODEL

from xscore.extraction.images import normalize_extracted_record
from xscore.extraction.profiles.base import ExamProfile
from xscore.extraction.providers.gemini import GeminiProvider
from xscore.extraction.providers.kimi import KimiProvider


def get_provider() -> GeminiProvider | KimiProvider:
    if AI_MODEL.startswith("kimi"):
        return KimiProvider()
    return GeminiProvider()


def create_extraction_client(api_key: str | None = None) -> Any | None:
    """Build API client for the configured ``AI_MODEL`` (Gemini or Kimi)."""
    if AI_MODEL.startswith("kimi"):
        return KimiProvider.create_client()
    key = api_key or os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    return genai.Client(api_key=key)


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


def multi_pass_extract(
    client: Any,
    image_bytes: bytes,
    page_num: int,
    profile: ExamProfile,
    passes: int,
    prompt_save_dir: Path | None = None,
) -> dict:
    """Run extraction multiple times and majority-vote all schema fields."""
    if passes <= 1:
        return call_ocr_api(client, image_bytes, page_num, profile, prompt_save_dir=prompt_save_dir)

    provider = get_provider()
    results: list[dict] = []
    for i in range(passes):
        results.append(
            provider.extract(
                client,
                image_bytes,
                profile.prompt,
                profile.schema,
                page_num,
                profile.answer_fields,
                prompt_save_dir=prompt_save_dir if i == 0 else None,
            )
        )
        if i < passes - 1:
            time.sleep(0.5)

    if all(r == results[0] for r in results):
        return results[0]

    all_fields = list(profile.schema.model_fields.keys())
    voted_result: dict = {"page_number": page_num}

    for field in all_fields:
        values = [r.get(field, "?") for r in results]
        if values:
            counter = Counter(values)
            most_common, count = counter.most_common(1)[0]
            if count >= len(values) / 2:
                voted_result[field] = most_common
            else:
                for r in results:
                    if r.get("confidence") == "high":
                        voted_result[field] = r.get(field, "?")
                        break
                else:
                    voted_result[field] = most_common

    if not all(r.get("confidence") == "high" for r in results):
        voted_result["confidence"] = "medium"

    return normalize_extracted_record(voted_result, profile.answer_fields)
