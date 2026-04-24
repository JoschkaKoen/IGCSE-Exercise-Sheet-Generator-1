"""Moonshot Kimi (OpenAI-compatible) vision extraction."""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from xscore.config import (
    AI_MODEL,
    KIMI_MAX_TOKENS,
    MAX_RETRIES,
    RETRY_BACKOFF_S,
    apply_kimi_k2_extra,
)
from xscore.extraction.images import normalize_extracted_record
from xscore.shared.response_parsing import parse_json_safe
from xscore.shared.terminal_ui import api_latency_line, log_ai_response_debug


try:
    from openai import OpenAI as _OpenAIClient

    KIMI_AVAILABLE = True
except ImportError:
    KIMI_AVAILABLE = False
    _OpenAIClient = None  # type: ignore[assignment,misc]



def _filter_schema_fields(data: dict, schema: type[BaseModel]) -> dict:
    """Remove extra fields not defined in the schema.

    Kimi sometimes adds extra fields like 'notes' or 'overall_confidence'
    that aren't in our schema. This filters them out.
    """
    allowed_fields = set(schema.model_fields.keys())
    return {k: v for k, v in data.items() if k in allowed_fields}




def _failed_record(last_error: Exception | str | None, answer_fields: list[str]) -> dict:
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


class KimiProvider:
    @staticmethod
    def create_client() -> Any | None:
        def _warn(msg: str) -> None:
            try:
                from xscore.shared.terminal_ui import warn_line
                warn_line(msg)
            except Exception:
                print(msg)

        if not KIMI_AVAILABLE:
            _warn("OpenAI package not installed. Run: pip install openai")
            return None
        api_key = os.getenv("KIMI_API_KEY")
        if not api_key:
            _warn("KIMI_API_KEY not set. Kimi will not be available.")
            return None

        base_url = os.getenv("KIMI_BASE_URL", "https://api.moonshot.cn/v1")

        assert _OpenAIClient is not None
        return _OpenAIClient(api_key=api_key, base_url=base_url)

    def extract(
        self,
        client: Any,
        image_bytes: bytes,
        prompt: str,
        schema: type[BaseModel],
        page_num: int,
        answer_fields: list[str],
        prompt_save_dir: Path | None = None,
    ) -> dict:
        if not KIMI_AVAILABLE or _OpenAIClient is None:
            return _failed_record("openai package not installed", answer_fields)
        if not isinstance(client, _OpenAIClient):
            try:
                from xscore.shared.terminal_ui import err_line

                err_line("Kimi model selected but wrong client type")
            except Exception:
                print("Error: Kimi model selected but wrong client type", file=sys.stderr)
            return _failed_record("Client type mismatch for Kimi", answer_fields)
        return self._single(client, image_bytes, page_num, prompt, schema, answer_fields,
                            prompt_save_dir=prompt_save_dir)

    def _single(
        self,
        client: Any,
        image_bytes: bytes,
        page_num: int,
        prompt: str,
        schema: type[BaseModel],
        answer_fields: list[str],
        prompt_save_dir: Path | None = None,
    ) -> dict:
        last_error: Exception | None = None
        backoff = RETRY_BACKOFF_S
        image_b64 = base64.b64encode(image_bytes).decode("utf-8")

        if prompt_save_dir is not None:
            from xscore.shared.prompt_logger import save_prompt
            save_prompt(
                prompt_save_dir / f"page_{page_num}.json",
                model=AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
            )

        # kimi-k2.5 has fixed temperature (1.0 thinking / 0.6 non-thinking);
        # passing any other value raises a 400 error.
        # For older moonshot-v1-* models, pass the configured temperature normally.
        # Retries: first sleep is RETRY_BACKOFF_S (default 1s), then doubling. The marking
        # pipeline uses 2**attempt seconds (2s, 4s) — intentional; see marking/kimi_helpers.

        for attempt in range(MAX_RETRIES + 1):
            try:
                kwargs: dict = dict(
                    model=AI_MODEL,
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
                    max_tokens=KIMI_MAX_TOKENS,
                    response_format={"type": "json_object"},
                )
                apply_kimi_k2_extra(AI_MODEL, kwargs, thinking=False)
                _t0 = time.perf_counter()
                response = client.chat.completions.create(**kwargs)
                api_latency_line(time.perf_counter() - _t0)
                raw = response.choices[0].message.content or ""
                log_ai_response_debug("kimi_extract", AI_MODEL, raw)
                try:
                    data = json.loads(raw)
                    # Filter out extra fields Kimi might add (notes, overall_confidence, etc.)
                    data = _filter_schema_fields(data, schema)
                    try:
                        schema.model_validate(data)
                    except ValidationError as val_err:
                        raise RuntimeError(
                            f"Kimi response failed schema validation for page {page_num}: {val_err}"
                        ) from val_err
                    return normalize_extracted_record(data, answer_fields)
                except json.JSONDecodeError as parse_err:
                    # Try to extract partial JSON
                    partial_data = parse_json_safe(raw)
                    if partial_data is not None:
                        # Also filter extra fields from partial data
                        partial_data = _filter_schema_fields(partial_data, schema)
                        try:
                            schema.model_validate(partial_data)
                        except ValidationError as val_err:
                            raise RuntimeError(
                                f"Kimi partial response failed schema validation for page {page_num}: {val_err}"
                            ) from val_err
                        return normalize_extracted_record(partial_data, answer_fields)

                    raise RuntimeError(f"Unparseable Kimi response for page {page_num}") from parse_err

            except Exception as e:
                try:
                    from xscore.shared.terminal_ui import warn_line

                    warn_line(
                        f"Kimi API error (attempt {attempt + 1}/{MAX_RETRIES + 1}): {e}"
                    )
                except Exception:
                    print(f"Kimi API error (attempt {attempt + 1}/{MAX_RETRIES + 1}): {e}")
                last_error = e

            if attempt < MAX_RETRIES:
                try:
                    from xscore.shared.terminal_ui import info_line

                    info_line(f"Retrying in {backoff}s…")
                except Exception:
                    print(f"Retrying in {backoff}s…")
                time.sleep(backoff)
                backoff *= 2

        return _failed_record(last_error, answer_fields)
