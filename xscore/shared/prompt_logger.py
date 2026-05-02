"""Save AI request prompts to files for debugging and auditing."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path
from typing import Any


_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}

_DATA_URL_RE = re.compile(r"^data:([^;]+);base64,(.*)$", re.DOTALL)


def _sidecar_stem(path: Path) -> str:
    """Stem to use for attachment sidecars next to *path*.

    ``Kim_page_8_prompt.txt`` → ``Kim_page_8`` (drop trailing ``_prompt``);
    ``page_5.json`` → ``page_5`` (no suffix to strip).
    """
    stem = path.stem
    if stem.endswith("_prompt"):
        stem = stem[: -len("_prompt")]
    return stem


def attachment_part(data: bytes, mime: str) -> dict:
    """OpenAI-shape ``image_url`` content part wrapping raw bytes as a
    base64 data URL. Used by callers that need to log binary attachments
    via :func:`save_prompt`. ``save_prompt`` keys off the data URL's mime
    to pick the sidecar extension, so this single helper covers all
    attachment types (jpg/png/pdf/…)."""
    return {
        "type": "image_url",
        "image_url": {
            "url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
        },
    }


def _decode_image_url(url: str) -> tuple[str, bytes] | None:
    """Decode a ``data:<mime>;base64,...`` URL into (mime, raw_bytes).

    Returns None for non-data-URL forms (e.g. ``http://...``) — those are
    rendered as a reference line without a sidecar.
    """
    match = _DATA_URL_RE.match(url)
    if not match:
        return None
    mime = match.group(1).strip()
    try:
        raw = base64.b64decode(match.group(2), validate=False)
    except (ValueError, TypeError):
        return None
    return mime, raw


def _cleanup_stale_sidecars(path: Path) -> None:
    """Remove any ``<stem>_attachment_*`` files next to *path* before writing
    fresh ones. Guards against stale leftovers from a prior run when
    ``--resume-dir`` reuses the same prompt path."""
    try:
        stem = _sidecar_stem(path)
        for old in path.parent.glob(f"{stem}_attachment_*"):
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


def save_prompt(
    path: Path | None,
    *,
    model: str = "",
    system: str = "",
    messages: list[dict[str, Any]],
) -> None:
    """Write an AI prompt to *path* as Markdown, with binary attachments
    emitted as sidecar files in the same directory.

    Text parts are inlined verbatim. ``image_url`` parts whose URL is a
    ``data:<mime>;base64,...`` form are decoded and written to
    ``<stem>_attachment_<NNN>.<ext>`` (extension by mime). The markdown
    body gets a one-line reference per attachment recording filename,
    mime, byte size, and a short sha256. Other / unknown part shapes
    emit a visible placeholder rather than being silently dropped.

    Numbering is global across all messages × all parts in send order,
    so the on-disk numbering matches API send order.

    Silently does nothing if *path* is ``None`` or on any I/O error so a
    logging fault never breaks the pipeline.
    """
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _cleanup_stale_sidecars(path)
        stem = _sidecar_stem(path)

        sections: list[str] = [f"# Prompt — {model}\n"]
        if system:
            sections.append(f"## system\n\n{system}\n")

        attachment_idx = 0
        for msg in messages:
            content = msg.get("content", "")
            role = msg.get("role", "user")
            body_lines: list[str] = []
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        body_lines.append(f"[unknown part type={type(part).__name__}]")
                        continue
                    ptype = part.get("type", "")
                    if ptype == "text":
                        body_lines.append(part.get("text", ""))
                        continue
                    if ptype == "image_url":
                        iu = part.get("image_url")
                        if isinstance(iu, dict):
                            url = iu.get("url", "")
                        elif isinstance(iu, str):
                            url = iu
                        else:
                            url = ""
                        decoded = _decode_image_url(url) if isinstance(url, str) else None
                        if decoded is None:
                            body_lines.append(f"[image_url] {url[:120]}")
                            continue
                        mime, raw = decoded
                        attachment_idx += 1
                        ext = _MIME_TO_EXT.get(mime, ".bin")
                        filename = f"{stem}_attachment_{attachment_idx:03d}{ext}"
                        sidecar = path.parent / filename
                        try:
                            sidecar.write_bytes(raw)
                        except OSError:
                            pass
                        sha = hashlib.sha256(raw).hexdigest()[:16]
                        body_lines.append(
                            f"[attachment {attachment_idx:03d}] {filename} · "
                            f"{mime} · {len(raw)} bytes · sha256:{sha}"
                        )
                        continue
                    body_lines.append(f"[unknown part type={ptype}]")
                text_only = "\n".join(body_lines)
            else:
                text_only = str(content)
            sections.append(f"## {role}\n\n{text_only}\n")

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

    Saves to ``<stem-without-_prompt>_response.txt``. When *thinking* is
    non-empty, the file body is:

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
        stem = _sidecar_stem(prompt_path)
        resp_path = prompt_path.with_name(f"{stem}_response.txt")
        resp_path.parent.mkdir(parents=True, exist_ok=True)
        resp_path.write_text(body, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _save_payload(
    prompt_path: Path | None, data: str | bytes, *, kind: str, ext: str,
) -> None:
    """Shared body for :func:`save_input_data` and :func:`save_output_data`.

    Writes *data* to ``<stem-without-_prompt>_<kind>.<ext>`` next to
    *prompt_path*. Strings are utf-8 encoded; bytes pass through. Silently
    no-ops on I/O error or when *prompt_path* is None — logging never breaks
    the pipeline."""
    if prompt_path is None:
        return
    try:
        stem = _sidecar_stem(prompt_path)
        out_path = prompt_path.with_name(f"{stem}_{kind}.{ext}")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            out_path.write_bytes(data)
        else:
            out_path.write_text(data, encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def save_input_data(
    prompt_path: Path | None, data: str | bytes, *, ext: str = "yaml",
) -> None:
    """Write the structured payload sent in the prompt body to
    ``<stem-without-_prompt>_input.<ext>`` next to *prompt_path*.

    *data* is the already-serialised string (or bytes) you embedded in the
    prompt — typically a blueprint YAML or scaffold stub. Caller picks
    *ext* (``"yaml"``, ``"json"``, ``"xml"``…). Pre-call save: pair with
    :func:`save_prompt` so the audit holds even if the API call fails.
    Silently no-ops on I/O error."""
    _save_payload(prompt_path, data, kind="input", ext=ext)


def save_output_data(
    prompt_path: Path | None, data: str | bytes, *, ext: str = "yaml",
) -> None:
    """Write the parsed AI response to
    ``<stem-without-_prompt>_output.<ext>`` next to *prompt_path*.

    *data* is the canonical re-serialised payload (or the AI's raw text if
    it's already in target format). Skip the call if the parser failed —
    :func:`save_response` already captured the raw string. Post-call save:
    pair with :func:`save_response` after a successful parse. Silently
    no-ops on I/O error."""
    _save_payload(prompt_path, data, kind="output", ext=ext)
