"""Opt-in response cache for the ai_marking step only.

Activated by ``ctx.instruction.reuse_cache == True``, which is set when the
user includes "reuse cache" / "use cache" in the natural-language prompt
(parsed in parse_grading_instructions). Default is OFF — running without the
phrase produces identical behaviour to the pre-cache pipeline.

Scope is deliberately narrow: only the OpenAI-compatible marking call in
``xscore.marking.mark_page._mark_page`` is cached today. The Gemini-native
PDF upload path (``xscore.marking.ai_mark._mark_page_pdf``, used only when
``MARKING_MODEL`` is gemini-* AND a student has continuation pages) is NOT
cached yet — the bytes-on-disk caching path for that route is intentional
future work. Scaffold parsing, name detection, cover-page checks, and every
other AI call still hit the API on every run.

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
