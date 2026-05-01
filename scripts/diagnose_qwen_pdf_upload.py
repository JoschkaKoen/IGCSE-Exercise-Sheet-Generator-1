"""Smoke test for Qwen native PDF input via DashScope file-extract.

Uploads the given PDF using ``eXercise.qwen_input.upload_pdf_for_extract``,
then runs a single chat completion that references it via the ``fileid://``
system message and prints the first 500 chars of the response.

Run:
    python scripts/diagnose_qwen_pdf_upload.py <path/to/sample.pdf>

Environment:
    QWEN_PDF_TEST_MODEL  Optional override. Defaults to "qwen-doc-turbo".
    DASHSCOPE_API_KEY    Required (loaded via env_load).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make sure repo root is on sys.path when running from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eXercise.env_load import load_project_env

load_project_env()

from eXercise.ai_client import make_ai_client
from eXercise.qwen_input import qwen_pdf_system_message, upload_pdf_for_extract


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <path/to/sample.pdf>", file=sys.stderr)
        return 2
    pdf_path = Path(sys.argv[1]).resolve()
    if not pdf_path.is_file():
        print(f"FAIL: not a file: {pdf_path}", file=sys.stderr)
        return 1

    result = make_ai_client(
        model_env="QWEN_PDF_TEST_MODEL",
        default_model="qwen-doc-turbo",
    )
    if result is None:
        print(
            "FAIL: make_ai_client returned None — check DASHSCOPE_API_KEY",
            file=sys.stderr,
        )
        return 1
    client, model, provider, _thinking, _max_tok = result
    print(f"client={type(client).__name__}  model={model}  provider={provider}")
    print(f"pdf={pdf_path}  ({pdf_path.stat().st_size:,} bytes)")
    print()

    print("Uploading PDF (purpose=file-extract) …")
    try:
        file_id = upload_pdf_for_extract(client, pdf_path)
    except Exception as exc:
        print(f"FAIL upload: {exc!r}", file=sys.stderr)
        return 1
    print(f"  → file_id = {file_id}")
    print()

    print("Calling chat.completions with fileid:// system message …")
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                qwen_pdf_system_message(file_id),
                {
                    "role": "user",
                    "content": (
                        "Briefly summarize the structure of this PDF in 3-5 sentences. "
                        "Mention the document title if visible, the number of pages, "
                        "and the top-level sections or questions."
                    ),
                },
            ],
            max_tokens=400,
        )
    except Exception as exc:
        print(f"FAIL chat: {exc!r}", file=sys.stderr)
        return 1

    content = resp.choices[0].message.content or "(empty)"
    print(f"PASS  {len(content)} chars returned")
    print()
    print("--- response (first 500 chars) ---")
    print(content[:500])
    if len(content) > 500:
        print(f"… (+{len(content) - 500} more)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
