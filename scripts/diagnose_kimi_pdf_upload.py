"""Smoke test for Kimi (Moonshot) native PDF input via the file-extract endpoint.

Uploads the given PDF using ``client.files.create(purpose="file-extract")``,
retrieves the server-extracted text via ``client.files.content(id).text``,
runs a single chat completion injecting that text as a system message, and
prints the first 500 chars of the response. Each step is wrapped separately
so the failure point (auth on upload vs. extraction vs. chat) is obvious.

Run:
    python scripts/diagnose_kimi_pdf_upload.py <path/to/sample.pdf>

Environment:
    KIMI_PDF_TEST_MODEL  Optional override. Defaults to "kimi-k2.6, 0, 1024"
                         (thinking off — required for non-streaming chat with
                         a small max_tokens, otherwise K2 burns the budget on
                         reasoning and returns an empty content string).
    KIMI_API_KEY         Required (loaded via env_load).
    KIMI_BASE_URL        Optional. Defaults to https://api.moonshot.cn/v1
                         (China). Set to https://api.moonshot.ai/v1 for the
                         international endpoint — keys are NOT interchangeable
                         between regions, so a 401 here usually means your key
                         is for the other endpoint.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make sure repo root is on sys.path when running from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eXercise.env_load import load_project_env

load_project_env()

from eXercise.ai_client import (
    build_completion_kwargs, collect_streamed_response, make_ai_client,
)


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <path/to/sample.pdf>", file=sys.stderr)
        return 2
    pdf_path = Path(sys.argv[1]).resolve()
    if not pdf_path.is_file():
        print(f"FAIL: not a file: {pdf_path}", file=sys.stderr)
        return 1

    result = make_ai_client(
        model_env="KIMI_PDF_TEST_MODEL",
        default_model="kimi-k2.6, 0, 1024",
    )
    if result is None:
        print(
            "FAIL: make_ai_client returned None — check KIMI_API_KEY",
            file=sys.stderr,
        )
        return 1
    client, model, provider, thinking_tokens, max_tokens = result
    base_url = os.environ.get("KIMI_BASE_URL", "").strip() or "https://api.moonshot.cn/v1"
    print(f"client={type(client).__name__}  model={model}  provider={provider}")
    print(f"base_url={base_url}")
    print(f"thinking_tokens={thinking_tokens}  max_tokens={max_tokens}")
    print(f"pdf={pdf_path}  ({pdf_path.stat().st_size:,} bytes)")
    print()

    if provider != "kimi":
        print(
            f"FAIL: model {model!r} routed to provider {provider!r}, not 'kimi'. "
            "Set KIMI_PDF_TEST_MODEL=kimi-k2-turbo-preview (or another kimi-*).",
            file=sys.stderr,
        )
        return 1

    print("Step 1/3: client.files.create(purpose='file-extract') …")
    try:
        file_obj = client.files.create(file=pdf_path, purpose="file-extract")
    except Exception as exc:
        print(f"FAIL upload: {exc!r}", file=sys.stderr)
        print(
            "\nHint: 401 / Invalid Authentication usually means the KIMI_API_KEY "
            "is for the *other* Moonshot endpoint. Keys issued at platform.moonshot.cn "
            "only work against api.moonshot.cn; keys issued at platform.moonshot.ai "
            "only work against api.moonshot.ai. Set KIMI_BASE_URL accordingly.",
            file=sys.stderr,
        )
        return 1
    file_id = file_obj.id
    print(f"  → file_id = {file_id}")
    print()

    print("Step 2/3: client.files.content(file_id).text …")
    try:
        extracted = client.files.content(file_id).text
    except Exception as exc:
        print(f"FAIL extract: {exc!r}", file=sys.stderr)
        try:
            client.files.delete(file_id)
        except Exception:
            pass
        return 1
    print(f"  → {len(extracted):,} chars extracted")
    print("    (first 300 chars):")
    print("    " + repr(extracted[:300]))
    print()

    # Apply the same thinking-toggle + streaming dispatch the pipeline uses.
    # K2 ids respect extra_body={"thinking": {...}}; older moonshot-v1-* ignore it.
    # When thinking is enabled, build_completion_kwargs returns use_stream=True
    # — matching the streaming branches in scaffold_detect.py /
    # scaffold_fill.py / scaffold_pages.py / scaffold_scheme.py.
    use_stream, completion_kw = build_completion_kwargs(
        provider, thinking_tokens, max_tokens or 1024,
    )
    print(f"Step 3/3: chat.completions.create  ·  stream={use_stream}  ·  kwargs={completion_kw}")

    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "system", "content": extracted},
        {
            "role": "user",
            "content": (
                "Briefly summarize the structure of this PDF in 3-5 sentences. "
                "Mention the document title if visible, the number of pages, "
                "and the top-level sections or questions."
            ),
        },
    ]

    import time as _time
    _t0 = _time.perf_counter()
    content = ""
    reasoning = ""
    finish: str | None = None
    usage = None
    try:
        if use_stream:
            stream = client.chat.completions.create(
                model=model, messages=messages, stream=True, **completion_kw,
            )
            _th: list[str] = []
            content_parts: list[str] = []
            chunk_count = 0
            first_chunk_t: float | None = None
            for chunk in stream:
                chunk_count += 1
                if first_chunk_t is None:
                    first_chunk_t = _time.perf_counter() - _t0
                u = getattr(chunk, "usage", None)
                if u is not None:
                    usage = u
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    content_parts.append(delta.content)
                r = getattr(delta, "reasoning_content", None)
                if r:
                    _th.append(r)
                fr = chunk.choices[0].finish_reason
                if fr:
                    finish = fr
            content = "".join(content_parts)
            reasoning = "".join(_th)
            print(
                f"  streamed  chunks={chunk_count}  "
                f"first_chunk_after={first_chunk_t:.2f}s  "
                f"total={_time.perf_counter() - _t0:.2f}s"
            )
        else:
            resp = client.chat.completions.create(
                model=model, messages=messages, **completion_kw,
            )
            print(f"  non-stream  total={_time.perf_counter() - _t0:.2f}s")
            msg = resp.choices[0].message
            content = msg.content or ""
            reasoning = getattr(msg, "reasoning_content", None) or ""
            finish = resp.choices[0].finish_reason
            usage = getattr(resp, "usage", None)
    except Exception as exc:
        print(f"FAIL chat: {exc!r}", file=sys.stderr)
        try:
            client.files.delete(file_id)
        except Exception:
            pass
        return 1

    print(f"  finish_reason = {finish}")
    if usage is not None:
        print(
            f"  usage: prompt={usage.prompt_tokens} "
            f"completion={usage.completion_tokens} "
            f"total={usage.total_tokens}"
        )
    print(f"  content       = {len(content):,} chars")
    print(f"  reasoning     = {len(reasoning):,} chars")
    if not content and not reasoning:
        print(
            "FAIL: chat returned no content and no reasoning_content. "
            "Check the model id and the thinking toggle.",
            file=sys.stderr,
        )
        try:
            client.files.delete(file_id)
        except Exception:
            pass
        return 1
    print()
    print(f"PASS  ({len(content)} content chars, {len(reasoning)} reasoning chars)")
    if content:
        print()
        print("--- content (first 500 chars) ---")
        print(content[:500])
        if len(content) > 500:
            print(f"… (+{len(content) - 500} more)")
    if reasoning:
        print()
        print("--- reasoning_content (first 300 chars) ---")
        print(reasoning[:300])
        if len(reasoning) > 300:
            print(f"… (+{len(reasoning) - 300} more)")

    # Cleanup — best effort.
    try:
        client.files.delete(file_id)
        print(f"\n(cleaned up file_id={file_id})")
    except Exception as exc:
        print(f"\n(cleanup failed for file_id={file_id}: {exc!r})", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
