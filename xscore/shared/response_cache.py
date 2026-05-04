"""Opt-in response cache for xscore AI calls.

Activated by ``ctx.instruction.reuse_cache == True``, which is set when the
user includes "reuse cache" / "use cache" in the natural-language prompt
(parsed in parse_grading_instructions). Default is OFF — running without the
phrase produces identical behaviour to the pre-cache pipeline.

Two activation surfaces:

1. ``xscore.marking.mark_page._mark_page`` calls the cache primitives
   directly (legacy call-site path) — preserved so existing entries stay
   bit-exact and the ``FormatParseError``-driven retry-after-stale logic that
   lives only in ``_mark_page`` keeps working.
2. ``eXercise.ai_client._TrackedCompletions.create`` and
   ``_TrackedGeminiModels.generate_content`` consult the cache when their
   wrapping client carries ``_should_cache=True``. This covers every AI
   call site whose factory was constructed with ``should_cache=...``,
   including the Gemini-native marking PDF upload path
   (``xscore.marking.ai_mark._mark_page_pdf``) and the scaffold/name-OCR/
   scheme-graphics steps.

Two paths intentionally skip caching even when the flag is on:

- **Qwen ``fileid://``** — DashScope mints a fresh file id per upload, so
  the system message differs every run and caching is futile.
  ``derive_oa_cache_key`` returns ``None`` for these calls.
- **Gemini Files-API (>18 MB PDFs)** — ``Part.from_uri`` carries only the
  URI; original bytes are gone before the wrapper sees the Part.
  ``derive_gemini_cache_key`` returns ``None`` for these calls.

Cache layout
------------
``~/.cache/xscore/responses/<key[:2]>/<key>.json`` — one file per cached
response. The two-character shard avoids putting tens of thousands of files
in a single directory. Each file contains:

    {
      "key":          "<sha256>",
      "model":        "qwen3.6-plus",
      "ts_written":   "2026-04-25T...Z",
      "response":     "<raw model output>",
      "tokens_in":    1234,
      "tokens_out":    567,
    }

Cache misses are silent (caller proceeds with the live API call). Read
errors are silent (treated as miss). Write errors are silent (next run will
just miss). Determinism is the user's friend here: with a fixed seed +
temperature=0 from item 4, identical inputs SHOULD produce identical outputs,
making the cache safe to reuse.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any

# `~/.cache/xscore/responses` follows XDG Base Dir conventions.
_DEFAULT_CACHE_ROOT = Path.home() / ".cache" / "xscore" / "responses"
_write_lock = threading.Lock()


def cache_root() -> Path:
    """Return the cache directory (override via ``XSCORE_CACHE_DIR`` env)."""
    override = os.environ.get("XSCORE_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return _DEFAULT_CACHE_ROOT


def cache_key(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    image_bytes: bytes | None = None,
    extra: str = "",
) -> str:
    """Return a stable SHA256 hex digest over the request inputs.

    *extra* lets callers fold in anything else that affects the response
    (e.g. a comma-joined list of attached image hashes when there are
    multiple). Order matters; pass the same components in the same order
    on every call to avoid spurious misses.
    """
    h = hashlib.sha256()
    h.update(b"model="); h.update(model.encode("utf-8")); h.update(b"\0")
    h.update(b"sys="); h.update(system_prompt.encode("utf-8")); h.update(b"\0")
    h.update(b"user="); h.update(user_prompt.encode("utf-8")); h.update(b"\0")
    if image_bytes is not None:
        h.update(b"img_sha256=")
        h.update(hashlib.sha256(image_bytes).hexdigest().encode("ascii"))
        h.update(b"\0")
    if extra:
        h.update(b"extra="); h.update(extra.encode("utf-8")); h.update(b"\0")
    return h.hexdigest()


def _path_for(key: str) -> Path:
    return cache_root() / key[:2] / f"{key}.json"


def cache_get(key: str) -> dict[str, Any] | None:
    """Return the cached entry dict or ``None`` on miss / read error."""
    path = _path_for(key)
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def cache_put(
    key: str,
    *,
    model: str,
    response: str,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
) -> None:
    """Persist a response. Best-effort: write errors are swallowed silently."""
    path = _path_for(key)
    payload: dict[str, Any] = {
        "key":        key,
        "model":      model,
        "ts_written": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
        "response":   response,
    }
    if tokens_in is not None:
        payload["tokens_in"] = tokens_in
    if tokens_out is not None:
        payload["tokens_out"] = tokens_out
    try:
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
    except OSError:
        pass


def derive_oa_cache_key(model: str, messages: list[dict]) -> str | None:
    """Derive a cache key from OpenAI-compat ``chat.completions`` kwargs.

    Walks *messages* to extract:

    - ``system_prompt`` — concatenation of every ``content`` string from
      ``role == "system"`` messages, in order.
    - ``user_prompt``   — concatenation of every ``text`` part from user
      messages (and any plain-string user content), in order.
    - ``image_bytes``   — base64-decoded bytes of the FIRST ``image_url``
      part (PDFs sent as ``data:application/pdf;base64,...`` count too).
    - ``extra``         — comma-joined sha256 hex of any subsequent
      ``image_url`` parts' decoded bytes.

    Returns ``None`` when the call shouldn't be cached: today that's any
    Qwen ``fileid://`` system message — DashScope mints a new file id per
    upload, so caching by id-as-text is futile.
    """
    sys_parts: list[str] = []
    user_parts: list[str] = []
    image_b64s: list[str] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            if isinstance(content, str):
                sys_parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("type") == "text":
                        sys_parts.append(p.get("text", "") or "")
            continue
        if role == "user":
            if isinstance(content, str):
                user_parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if not isinstance(p, dict):
                        continue
                    ptype = p.get("type")
                    if ptype == "text":
                        user_parts.append(p.get("text", "") or "")
                    elif ptype == "image_url":
                        url = (p.get("image_url") or {}).get("url", "")
                        if isinstance(url, str) and "," in url and url.startswith("data:"):
                            image_b64s.append(url.split(",", 1)[1])

    system_prompt = "\n".join(sys_parts)
    if "fileid://" in system_prompt:
        # Qwen DashScope file-extract upload mode — file id changes per run.
        return None

    user_prompt = "\n".join(user_parts)

    def _decode(b64: str) -> bytes:
        try:
            import base64
            return base64.b64decode(b64)
        except Exception:
            return b""

    image_bytes: bytes | None = None
    extra = ""
    if image_b64s:
        image_bytes = _decode(image_b64s[0])
        if len(image_b64s) > 1:
            extras = [
                hashlib.sha256(_decode(b)).hexdigest() for b in image_b64s[1:]
            ]
            extra = ",".join(extras)

    return cache_key(
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        image_bytes=image_bytes,
        extra=extra,
    )


def derive_gemini_cache_key(model: str, contents: Any, config: Any) -> str | None:
    """Derive a cache key from native Gemini ``generate_content`` kwargs.

    Walks *contents* (a list of ``Part``s — or a single Part) to extract
    text parts and inline-data (PDF / image) parts. Pulls
    ``system_instruction`` from *config* when present.

    Returns ``None`` when any Part is a Files-API URI
    (``Part.from_uri`` for >18 MB PDFs) — the original bytes are gone by
    the time the wrapper sees the Part, so caching is unsupported.
    """
    if contents is None:
        contents_list: list = []
    elif isinstance(contents, (list, tuple)):
        contents_list = list(contents)
    else:
        contents_list = [contents]

    system_prompt = ""
    if config is not None:
        sys_instr = getattr(config, "system_instruction", None)
        if isinstance(sys_instr, str):
            system_prompt = sys_instr

    user_parts: list[str] = []
    image_bytes_list: list[bytes] = []
    for part in contents_list:
        # Plain-string content is permitted; treat as text.
        if isinstance(part, str):
            user_parts.append(part)
            continue
        # Files-API URI part — bytes unrecoverable.
        file_data = getattr(part, "file_data", None)
        if file_data is not None and getattr(file_data, "file_uri", None):
            return None
        text = getattr(part, "text", None)
        if isinstance(text, str) and text:
            user_parts.append(text)
            continue
        inline = getattr(part, "inline_data", None)
        if inline is not None:
            data = getattr(inline, "data", None)
            if isinstance(data, (bytes, bytearray)) and data:
                image_bytes_list.append(bytes(data))

    image_bytes: bytes | None = None
    extra = ""
    if image_bytes_list:
        image_bytes = image_bytes_list[0]
        if len(image_bytes_list) > 1:
            extras = [hashlib.sha256(b).hexdigest() for b in image_bytes_list[1:]]
            extra = ",".join(extras)

    return cache_key(
        model=model,
        system_prompt=system_prompt,
        user_prompt="\n".join(user_parts),
        image_bytes=image_bytes,
        extra=extra,
    )


def reuse_cache_enabled(ctx: Any) -> bool:
    """True iff the user opted in via the NL prompt (or env override).

    Honours ``XSCORE_REUSE_CACHE=1`` as an env-var override for ad-hoc testing
    without re-issuing the natural-language prompt; otherwise the only way to
    enable the cache is the ``reuse_cache`` flag set by the step-1 NL parser.
    """
    if os.environ.get("XSCORE_REUSE_CACHE", "").strip() in ("1", "true", "yes"):
        return True
    instr = getattr(ctx, "instruction", None)
    if instr is None:
        return False
    return bool(getattr(instr, "reuse_cache", False))
