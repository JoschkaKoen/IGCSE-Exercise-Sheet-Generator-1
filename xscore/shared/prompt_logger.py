"""Save AI request prompts to files for debugging and auditing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def save_prompt(
    path: Path | None,
    *,
    model: str = "",
    system: str = "",
    messages: list[dict[str, Any]],
) -> None:
    """Write the text portions of an AI prompt to *path* as JSON.

    Strips image data (base64 ``image_url`` items, binary parts) — only text
    content is saved.  Silently does nothing if *path* is ``None`` or if any
    I/O error occurs, so this never crashes the pipeline.
    """
    if path is None:
        return
    try:
        cleaned: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [
                    part["text"]
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                text_only = " ".join(texts)
            else:
                text_only = str(content)
            cleaned.append({"role": msg.get("role", "user"), "content": text_only})

        data: dict[str, Any] = {"model": model, "system": system, "messages": cleaned}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
