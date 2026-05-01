"""Diagnose the DashScope/Qwen 'System message must be at the beginning' 400.

We saw a 400 from `response_format={"type": "json_schema", ...}` on
`qwen3.6-flash` even though the messages already had system at the beginning.
This script isolates which factor (schema strict-compliance, `strict=True`,
system message presence) actually triggers the rejection by varying one factor
at a time.

Run:
    python scripts/diagnose_qwen_json_schema.py
"""

from __future__ import annotations

import base64
import sys
from io import BytesIO

# Make sure repo root is on sys.path when running from scripts/
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from PIL import Image

# Load default.env + .env exactly like xScore.py does (env_load handles ordering).
from eXercise.env_load import load_project_env

load_project_env()

from eXercise.ai_client import make_ai_client
from xscore.scaffold.scaffold_prompts import _LAYOUT_DETECT_JSON_SCHEMA


def _tiny_image_b64() -> str:
    """100x100 white JPEG — content doesn't matter, we're testing transport."""
    img = Image.new("RGB", (100, 100), "white")
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def main() -> int:
    result = make_ai_client(model_env="DETECT_LAYOUT_MODEL")
    if result is None:
        print("FAIL: make_ai_client returned None — check DETECT_LAYOUT_MODEL and API key")
        return 1
    client, model, provider, _, _ = result
    print(f"client={type(client).__name__}  model={model}  provider={provider}")
    print()

    img_b64 = _tiny_image_b64()
    sys_text = "You are an expert at identifying exam paper printing layouts."
    user_text = (
        "Look at this image. Return JSON: "
        '{"rows":1,"cols":1,"reading_order":[[1,1]]}'
    )

    msgs_with_system = [
        {"role": "system", "content": sys_text},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user_text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                },
            ],
        },
    ]
    msgs_no_system = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": sys_text + "\n\n" + user_text},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                },
            ],
        },
    ]

    schema_pydantic = _LAYOUT_DETECT_JSON_SCHEMA  # current failing schema
    schema_strict = {
        "type": "object",
        "properties": {
            "rows": {"type": "integer"},
            "cols": {"type": "integer"},
            "reading_order": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "integer"}},
            },
        },
        "required": ["rows", "cols", "reading_order"],
        "additionalProperties": False,
    }

    def run(name: str, msgs: list, response_format: dict) -> None:
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=msgs,
                response_format=response_format,
                max_tokens=200,
            )
            content = resp.choices[0].message.content or "(empty)"
            print(f"PASS  {name}")
            print(f"      → {content!r}")
        except Exception as exc:
            print(f"FAIL  {name}")
            # Single-line error so the matrix is readable
            print(f"      → {str(exc).splitlines()[0]}")
        print()

    # Test matrix
    run(
        "1. with-system, schema=pydantic, strict=True   (the failing case)",
        msgs_with_system,
        {"type": "json_schema", "json_schema": {"name": "layout", "schema": schema_pydantic, "strict": True}},
    )
    run(
        "2. with-system, schema=strict,   strict=True",
        msgs_with_system,
        {"type": "json_schema", "json_schema": {"name": "layout", "schema": schema_strict, "strict": True}},
    )
    run(
        "3. with-system, schema=pydantic, strict=False",
        msgs_with_system,
        {"type": "json_schema", "json_schema": {"name": "layout", "schema": schema_pydantic, "strict": False}},
    )
    run(
        "4. no-system,   schema=pydantic, strict=True",
        msgs_no_system,
        {"type": "json_schema", "json_schema": {"name": "layout", "schema": schema_pydantic, "strict": True}},
    )
    run(
        "5. with-system, json_object   (baseline — known to work)",
        msgs_with_system,
        {"type": "json_object"},
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
