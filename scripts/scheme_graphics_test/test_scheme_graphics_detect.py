"""Standalone test: detect mark-scheme graphics via Gemini Flash Lite PDF upload.

Usage:
    python test_scheme_graphics_detect.py

Uploads the mark scheme PDF to the Gemini Files API and asks Flash Lite to
return a JSON list of {page, question_number} entries for every question whose
expected answer includes a diagram or graph (not a table).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv("default.env")
load_dotenv()

PDF_PATH = Path("/Users/joschka/Desktop/IGCSE Computer Science 25/Scanned Exams/s23 12/CS s23 12 Ex. all_answers.pdf")

MODEL = "gemini-2.5-flash-lite"

PROMPT = (
    "You are analysing a mark scheme PDF.\n\n"
    "Your task: identify every question whose expected answer includes a diagram, graph, "
    "or image (do NOT include tables — only visual figures such as flowcharts, circuit diagrams, "
    "graphs, maps, drawings, etc.).\n\n"
    "For each such question output one JSON object with two fields:\n"
    '  "page"            — 1-based page number in this PDF where the graphic appears\n'
    '  "question_number" — the question number as printed in the mark scheme (e.g. "3(b)(ii)")\n\n'
    "Return ONLY a JSON array of these objects, no markdown fences, no surrounding text.\n"
    'If no graphics are found, return an empty array: []\n\n'
    "Example output:\n"
    '[{"page": 2, "question_number": "1(b)"}, {"page": 5, "question_number": "4(a)(i)"}]'
)


def main() -> None:
    api_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
    if not api_key:
        raise SystemExit("GEMINI_API_KEY not set in .env")

    from google import genai as gai
    from google.genai import types as gai_types

    client = gai.Client(api_key=api_key)

    print(f"Uploading {PDF_PATH.name} …")
    f = client.files.upload(
        file=PDF_PATH,
        config=gai_types.UploadFileConfig(mime_type="application/pdf"),
    )
    for _ in range(120):
        state = getattr(f.state, "name", str(f.state))
        if state != "PROCESSING":
            break
        time.sleep(3)
        f = client.files.get(name=f.name)
    else:
        raise SystemExit("Upload timed out after 6 minutes")

    state = getattr(f.state, "name", str(f.state))
    if state == "FAILED":
        raise SystemExit(f"Gemini file processing failed: {f.name}")
    print(f"Upload done  ·  state={state}  ·  uri={f.uri}")

    print(f"Calling {MODEL} …")
    resp = client.models.generate_content(
        model=MODEL,
        contents=[
            gai_types.Part.from_uri(file_uri=f.uri, mime_type="application/pdf"),
            gai_types.Part.from_text(text=PROMPT),
        ],
        config=gai_types.GenerateContentConfig(
            max_output_tokens=4096,
        ),
    )
    raw = (resp.text or "").strip()
    print("\n--- Raw response ---")
    print(raw)
    print("---")

    try:
        data = json.loads(raw)
        print(f"\nParsed: {len(data)} graphic(s) found")
        for item in data:
            print(f"  page {item['page']:>3}  ·  Q {item['question_number']}")
    except json.JSONDecodeError as e:
        print(f"\nJSON parse failed: {e}")


if __name__ == "__main__":
    main()
