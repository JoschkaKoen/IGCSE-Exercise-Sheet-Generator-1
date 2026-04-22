"""Save AI request prompts to files for debugging and auditing."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def save_prompt(
    path: Path | None,
    *,
    model: str = "",
    system: str = "",
    messages: list[dict[str, Any]],
) -> None:
    """Write the text portions of an AI prompt to *path* as Markdown.

    Strips image data (base64 ``image_url`` items, binary parts) — only text
    content is saved.  Silently does nothing if *path* is ``None`` or if any
    I/O error occurs, so this never crashes the pipeline.
    """
    if path is None:
        return
    try:
        sections: list[str] = [f"# Prompt — {model}\n"]
        if system:
            sections.append(f"## system\n\n{system}\n")
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                texts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ]
                text_only = "\n".join(texts)
            else:
                text_only = str(content)
            role = msg.get("role", "user")
            sections.append(f"## {role}\n\n{text_only}\n")

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(sections), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def save_response(prompt_path: Path | None, response: str) -> None:
    """Write raw AI response text alongside *prompt_path*.

    Saves to <stem>_response.txt in the same directory.
    Silently does nothing if path is None or on any I/O error.
    """
    if prompt_path is None:
        return
    try:
        resp_path = prompt_path.with_name(prompt_path.stem + "_response.txt")
        resp_path.parent.mkdir(parents=True, exist_ok=True)
        resp_path.write_text(response, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def save_thinking(prompt_path: Path | None, thinking: str) -> None:
    """Write AI thinking/reasoning text alongside *prompt_path*.

    Saves to <stem>_thinking.txt in the same directory.
    Silently does nothing if path is None, thinking is empty, or on I/O error.
    """
    if prompt_path is None or not thinking:
        return
    try:
        thinking_path = prompt_path.with_name(prompt_path.stem + "_thinking.txt")
        thinking_path.parent.mkdir(parents=True, exist_ok=True)
        thinking_path.write_text(thinking, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
