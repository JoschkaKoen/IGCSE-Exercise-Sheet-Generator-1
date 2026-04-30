"""Google Gemini vision extraction (single call, ensemble, multi-pass voting)."""

from __future__ import annotations

import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from xscore.config import (
    AI_MODEL,
    ENSEMBLE_CALLS,
    GEMINI_MAX_OUTPUT_TOKENS,
    GEMINI_TEMPERATURE,
    MAX_RETRIES,
    RETRY_BACKOFF_S,
    USE_ENSEMBLE,
)
from xscore.extraction.images import failed_extraction_record, normalize_extracted_record


class GeminiProvider:
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
        from google import genai
        if not isinstance(client, genai.Client):
            from xscore.shared.terminal_ui import err_line

            err_line("Gemini model selected but wrong client type")
            return failed_extraction_record("Client type mismatch for Gemini", answer_fields)
        if USE_ENSEMBLE:
            return self._ensemble(client, image_bytes, page_num, prompt, schema, answer_fields, ENSEMBLE_CALLS,
                                  prompt_save_dir=prompt_save_dir)
        return self._single(client, image_bytes, page_num, prompt, schema, answer_fields,
                            prompt_save_dir=prompt_save_dir)

    def _single(
        self,
        client: genai.Client,
        image_bytes: bytes,
        page_num: int,
        prompt: str,
        schema: type[BaseModel],
        answer_fields: list[str],
        prompt_save_dir: Path | None = None,
    ) -> dict:
        from google import genai
        from google.genai import types

        if prompt_save_dir is not None:
            from xscore.shared.prompt_logger import save_prompt
            save_prompt(
                prompt_save_dir / f"page_{page_num}.json",
                model=AI_MODEL,
                messages=[{"role": "user", "content": prompt}],
            )

        gen_config = types.GenerateContentConfig(
            temperature=GEMINI_TEMPERATURE,
            max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
            response_mime_type="application/json",
            response_schema=schema,
        )

        from eXercise.api_retry import retry_api_call

        def _do_call() -> dict:
            _t0 = time.perf_counter()
            response = client.models.generate_content(
                model=AI_MODEL,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    prompt,
                ],
                config=gen_config,
            )
            from xscore.shared.terminal_ui import api_latency_line
            api_latency_line(time.perf_counter() - _t0)
            try:
                finish_reason = response.candidates[0].finish_reason
            except (IndexError, AttributeError):
                finish_reason = "unknown"
            if response.parsed:
                return normalize_extracted_record(response.parsed.model_dump(), answer_fields)
            raw = response.text or ""
            from rich.panel import Panel

            from xscore.shared.terminal_ui import get_console

            get_console().print(
                Panel(
                    f"finish_reason={finish_reason}\n\n{raw}",
                    title=f"[DEBUG] full response ({len(raw)} chars)",
                    border_style="dim",
                )
            )
            try:
                return normalize_extracted_record(json.loads(raw), answer_fields)
            except (json.JSONDecodeError, ValueError) as parse_err:
                raise RuntimeError(
                    f"Unparseable response for page {page_num} (finish_reason={finish_reason})"
                ) from parse_err

        try:
            return retry_api_call(
                _do_call,
                label=f"Gemini extract p{page_num}",
                max_attempts=MAX_RETRIES + 1,
                base_sleep=RETRY_BACKOFF_S,
                backoff_factor=2.0,
                jitter=0.0,
            )
        except Exception as e:
            return failed_extraction_record(e, answer_fields)

    def _ensemble(
        self,
        client: genai.Client,
        image_bytes: bytes,
        page_num: int,
        prompt: str,
        schema: type[BaseModel],
        answer_fields: list[str],
        num_calls: int,
        prompt_save_dir: Path | None = None,
    ) -> dict:
        results: list[dict] = []
        for i in range(num_calls):
            results.append(self._single(client, image_bytes, page_num, prompt, schema, answer_fields,
                                        prompt_save_dir=prompt_save_dir if i == 0 else None))

        if len(results) == 1:
            return results[0]

        final_result = results[0].copy()

        for field in answer_fields:
            votes = [r.get(field, "?") for r in results]
            vote_counts = Counter(votes)
            winner = vote_counts.most_common(1)[0][0]
            final_result[field] = winner

            agreement = vote_counts[winner] / len(votes)
            if agreement == 1.0:
                final_result[f"{field}_confidence"] = "high"
            elif agreement >= 0.5:
                final_result[f"{field}_confidence"] = "medium"
            else:
                final_result[f"{field}_confidence"] = "low"

        names = [
            r.get("student_name", "UNKNOWN")
            for r in results
            if r.get("student_name") not in ("UNKNOWN", "EXTRACTION_ERROR", "?")
        ]
        if names:
            name_counts = Counter(names)
            winner_name, winner_votes = name_counts.most_common(1)[0]
            final_result["student_name"] = winner_name
            name_agreement = winner_votes / len(names)
            if name_agreement == 1.0:
                final_result["student_name_confidence"] = "high"
            elif name_agreement >= 0.5:
                final_result["student_name_confidence"] = "medium"
            else:
                final_result["student_name_confidence"] = "low"

        confidences = [r.get("confidence", "low") for r in results]
        conf_counts = Counter(confidences)
        final_result["confidence"] = conf_counts.most_common(1)[0][0]

        return final_result
