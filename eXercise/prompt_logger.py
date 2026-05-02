"""Save AI request prompts / responses / thinking to files for debugging.

Local copy in the eXercise package so the exercise-sheet pipeline does not
depend on the xscore (marking) package. ``xscore.shared.prompt_logger`` is
the marker-side counterpart; it diverges from this copy in that it writes
binary attachments (page scans, scheme PDFs, scheme graphics) to sidecar
files for full audit fidelity. This eXercise copy stays text-only — the
exercise-sheet pipeline does not currently send images that warrant audit.
"""

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


def save_response(
    prompt_path: Path | None,
    response: str,
    *,
    thinking: str | None = None,
) -> None:
    """Write the AI response (optionally prefixed with thinking) alongside *prompt_path*.

    Saves to <stem>_response.txt. When *thinking* is non-empty, the file body is:

        [thinking]
        <thinking>
        [/thinking]

        <response>

    Empty/None thinking is omitted entirely, so the file is identical to the
    plain-response form. Silently does nothing if path is None or on I/O error.
    """
    if prompt_path is None:
        return
    try:
        if thinking:
            body = f"[thinking]\n{thinking}\n[/thinking]\n\n{response}"
        else:
            body = response
        resp_path = prompt_path.with_name(prompt_path.stem + "_response.txt")
        resp_path.parent.mkdir(parents=True, exist_ok=True)
        resp_path.write_text(body, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
