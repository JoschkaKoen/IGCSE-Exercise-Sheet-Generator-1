"""Probe gemini-3-flash-preview on the step-14 handwriting prompt.

Why: in the cross-model handwriting comparison, gemini-3-flash-preview returned
all-inconclusive (parsed empty). The ``_call_handwriting`` Gemini path passes
no ``thinking_config`` (so thoughts default ON) and caps
``max_output_tokens=192`` — a budget that may be entirely consumed by hidden
thinking on a thinking-by-default model, leaving zero tokens for the JSON
answer.

This script reproduces the exact production call, then reruns with variants
(thinking off, larger budget) to pinpoint the cause and confirm the fix.

Run:
    python scripts/diagnose_gemini_handwriting.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eXercise.env_load import load_project_env

load_project_env()

from xscore.marking.blank_page_detection import _render_page_jpeg

PDF = Path(
    "output/xscore/s23_12/2026-05-03_01-50-02/04_merge_duplex_scans/merged_scan.pdf"
)
PAGE = 36  # the page with the false-positive in the original run
MODEL = "gemini-3-flash-preview"


def call(jpeg: bytes, prompt: str, *, max_tokens: int, thinking: int | None) -> None:
    from google.genai import types as gai_types
    from eXercise.ai_client import (
        build_gemini_thinking_config,
        make_gemini_native_client,
        split_gemini_response,
    )
    from xscore.marking.blank_page_detection import _HandwritingPageNumberResp

    client = make_gemini_native_client()
    cfg_kwargs: dict = {
        "max_output_tokens": max_tokens,
        "response_mime_type": "application/json",
        "response_schema": _HandwritingPageNumberResp,
    }
    if thinking is not None:
        cfg_kwargs["thinking_config"] = build_gemini_thinking_config(thinking)
    label = f"max_tokens={max_tokens}, thinking={thinking}"
    print(f"── {label} {'─' * (60 - len(label))}")
    resp = client.models.generate_content(
        model=MODEL,
        contents=[
            gai_types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"),
            gai_types.Part.from_text(text=prompt),
        ],
        config=gai_types.GenerateContentConfig(**cfg_kwargs),
    )

    answer, thinking_text = split_gemini_response(resp)
    finish = None
    candidates = getattr(resp, "candidates", None) or []
    if candidates:
        finish = getattr(candidates[0], "finish_reason", None)
    usage = getattr(resp, "usage_metadata", None)
    print(f"  finish_reason: {finish}")
    if usage is not None:
        for attr in ("prompt_token_count", "candidates_token_count",
                     "thoughts_token_count", "total_token_count"):
            v = getattr(usage, attr, None)
            if v is not None:
                print(f"  {attr}: {v}")
    print(f"  thinking_text len: {len(thinking_text)}")
    print(f"  answer len:        {len(answer)}")
    if answer:
        print(f"  answer (first 300): {answer[:300]!r}")
    if thinking_text:
        print(f"  thinking (first 200): {thinking_text[:200]!r}")
    print()


def main() -> int:
    if not PDF.is_file():
        print(f"FAIL: {PDF}", file=sys.stderr)
        return 1

    from xscore.prompts.loader import load_prompt
    _, prompt = load_prompt("student_handwriting_check")
    prompt = prompt.rstrip("\n")
    jpeg = _render_page_jpeg(PDF, PAGE)
    print(f"PDF: {PDF}\nPage: {PAGE}  jpeg={len(jpeg)} bytes  prompt={len(prompt)} chars\n")

    # Variant 1: production settings (max=192, thinking=None → default ON)
    call(jpeg, prompt, max_tokens=192, thinking=None)
    # Variant 2: production max, thinking off
    call(jpeg, prompt, max_tokens=192, thinking=0)
    # Variant 3: bigger budget, thinking on (default)
    call(jpeg, prompt, max_tokens=4096, thinking=None)
    # Variant 4: bigger budget, thinking off
    call(jpeg, prompt, max_tokens=4096, thinking=0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
