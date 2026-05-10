# -*- coding: utf-8 -*-
"""Shared LLM client factory.

Provider is inferred automatically from the model name — no separate
AI_PROVIDER setting is needed.

Supported providers (auto-detected by model name prefix)
---------------------------------------------------------
gemini  (model names starting with ``gemini``)
    base_url : https://generativelanguage.googleapis.com/v1beta/openai/
    api_key  : GEMINI_API_KEY  (GOOGLE_API_KEY accepted as fallback)
    example  : gemini-2.5-flash, gemini-2.0-flash

xai  (model names starting with ``grok``)
    base_url : https://api.x.ai/v1
    api_key  : XAI_API_KEY
    example  : grok-4-1-fast-non-reasoning, grok-3

qwen  (model names starting with ``qwen``)
    base_url : https://dashscope.aliyuncs.com/compatible-mode/v1
    api_key  : DASHSCOPE_API_KEY
    example  : qwen3.6-plus, qwen3-32b
    note     : Thinking on → streaming required; thinking off → non-streaming.

kimi  (model names starting with ``kimi`` or ``moonshot``)
    base_url : https://api.moonshot.cn/v1  (China endpoint — default,
               matches the existing xscore.extraction kimi path. Override
               via KIMI_BASE_URL=https://api.moonshot.ai/v1 for the
               international endpoint. Keys are NOT interchangeable.)
    api_key  : KIMI_API_KEY
    example  : kimi-k2.6, kimi-k2-turbo-preview, moonshot-v1-128k
    note     : Native PDF input via files.create(purpose="file-extract")
               returns plain text, injected as a system message — see
               kimi_pdf_text() below. K2 thinking is binary on/off via
               extra_body. K2 has fixed temperature, so determinism
               injection is skipped for kimi-k2* ids.

Per-call-type model overrides
------------------------------
Each env var accepts an optional thinking-effort suffix after a comma:

    AI_DEFAULT_MODEL=gemini-2.5-flash          # model only (provider default thinking)
    NL_MODEL=gemini-2.5-flash, low             # model + effort
    AI_PRECHECK_MODEL=gemini-2.5-flash-lite, off

Accepted effort values:  off | low | high  (omit = provider default)

    AI_DEFAULT_MODEL   fallback model (and effort) for all calls
    NL_MODEL           prompt interpretation  (overrides AI_DEFAULT_MODEL)
    MCQ_MODEL          AI explanation generation (overrides AI_DEFAULT_MODEL)

Environment variables (API keys)
---------------------------------
GEMINI_API_KEY    Required for gemini models  (GOOGLE_API_KEY accepted as fallback)
XAI_API_KEY       Required for grok models
DASHSCOPE_API_KEY Required for qwen models
KIMI_API_KEY      Required for kimi / moonshot models  (KIMI_BASE_URL optional)
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Thread-safe token-usage + call-stats accumulators
# ---------------------------------------------------------------------------

_usage_lock = threading.Lock()
_run_usage: dict[str, dict[str, int]] = {}  # model → {"input": N, "output": N, "thinking": N}
_run_call_stats: dict[str, dict[str, float]] = {}  # model → {"calls": N, "total_duration_s": F}


# ---------------------------------------------------------------------------
# Determinism — fixed seed + temperature for reproducible runs
# ---------------------------------------------------------------------------
#
# Both env vars are read fresh on every call so a caller can change them mid-run
# (e.g. for ad-hoc experiments). Empty / unparseable values disable injection
# of that param, leaving provider defaults in place.
#
# Per-call kwargs always win — passing ``temperature=0.7`` or ``seed=...`` to
# ``chat.completions.create`` (or supplying a ``config`` with those fields set
# to a non-None value for native Gemini) is honoured unchanged.

def _read_default_temperature() -> float | None:
    raw = os.environ.get("ALL_AI_TEMPERATURE", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _read_default_seed() -> int | None:
    raw = os.environ.get("ALL_AI_SEED", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def record_usage(
    model: str,
    input_tokens: int,
    output_tokens: int,
    thinking_tokens: int = 0,
) -> None:
    """Accumulate token counts for *model* (thread-safe).

    *output_tokens* is the total billed output (visible + thinking), matching
    what the provider invoices. *thinking_tokens* is the thinking portion of
    *output_tokens* — informational only, not used in cost arithmetic.
    """
    with _usage_lock:
        e = _run_usage.setdefault(model, {"input": 0, "output": 0, "thinking": 0})
        e["input"] += input_tokens
        e["output"] += output_tokens
        e["thinking"] += thinking_tokens


def _extract_reasoning_tokens(u: Any) -> int:
    """Return reasoning/thinking token count from an OpenAI-compat usage object.

    Tries ``usage.completion_tokens_details.reasoning_tokens`` first (the
    OpenAI standard, used by Gemini OpenAI-compat and recent Qwen3 reasoning
    models), then falls back to a flat ``usage.reasoning_tokens`` shape some
    DashScope versions emit. Returns 0 if neither is present.
    """
    details = getattr(u, "completion_tokens_details", None)
    nested = (getattr(details, "reasoning_tokens", 0) if details else 0) or 0
    if nested:
        return nested
    return getattr(u, "reasoning_tokens", 0) or 0


def get_run_usage() -> dict[str, dict[str, int]]:
    """Return a snapshot of accumulated token counts since last :func:`reset_run_usage`."""
    with _usage_lock:
        return {m: dict(v) for m, v in _run_usage.items()}


def reset_run_usage() -> None:
    """Clear all accumulated token counts. Call at pipeline start to isolate runs."""
    with _usage_lock:
        _run_usage.clear()


def record_call(model: str, duration_s: float) -> None:
    """Accumulate one successful API call for *model* (thread-safe).

    Failed attempts are not counted — retry wall-time appears in per-step
    timings (step 28), so per-call averages stay meaningful.
    """
    with _usage_lock:
        e = _run_call_stats.setdefault(model, {"calls": 0.0, "total_duration_s": 0.0})
        e["calls"] += 1
        e["total_duration_s"] += duration_s


def get_run_call_stats() -> dict[str, dict[str, float]]:
    """Return a snapshot of accumulated call stats since last :func:`reset_run_call_stats`."""
    with _usage_lock:
        return {m: dict(v) for m, v in _run_call_stats.items()}


def reset_run_call_stats() -> None:
    """Clear all accumulated call stats. Call at pipeline start to isolate runs."""
    with _usage_lock:
        _run_call_stats.clear()


@dataclass(frozen=True)
class _ProviderDef:
    """Immutable descriptor for a single LLM provider."""
    name: str
    base_url: str
    api_key_env: str
    model_prefixes: tuple[str, ...]  # first match against model name prefix wins


# Registry of known providers. To add a new provider, append one entry here.
_PROVIDER_REGISTRY: list[_ProviderDef] = [
    _ProviderDef(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key_env="GEMINI_API_KEY",
        model_prefixes=("gemini",),
    ),
    _ProviderDef(
        name="xai",
        base_url="https://api.x.ai/v1",
        api_key_env="XAI_API_KEY",
        model_prefixes=("grok",),
    ),
    _ProviderDef(
        name="qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
        model_prefixes=("qwen",),
    ),
    _ProviderDef(
        name="kimi",
        # Default to .cn endpoint (matches xscore.extraction.providers.kimi).
        # Override to https://api.moonshot.ai/v1 via KIMI_BASE_URL for the
        # international endpoint. Keys are region-specific and NOT
        # interchangeable. Resolved lazily in make_ai_client() so tests /
        # runtime overrides always win.
        base_url="https://api.moonshot.cn/v1",
        api_key_env="KIMI_API_KEY",
        model_prefixes=("kimi", "moonshot"),
    ),
]

# Fallback model when no model env var is set anywhere.
_DEFAULT_MODEL = "gemini-2.5-flash"


def provider_for_model(model: str) -> str:
    """Return the provider name for *model* based on its name prefix.

    Falls back to ``gemini`` for unknown model names.
    """
    m = model.lower()
    for pdef in _PROVIDER_REGISTRY:
        if any(m.startswith(pfx) for pfx in pdef.model_prefixes):
            return pdef.name
    return "gemini"


# Providers that reject ``response_format={"type": "json_schema", ...}`` when
# a system message is present. DashScope (Qwen) is the known case — verified
# via scripts/diagnose_qwen_json_schema.py: schema strictness, the ``strict``
# flag, and ``default`` fields are all irrelevant; the rejection is purely
# triggered by having a system message in the conversation. The error text
# DashScope returns ("System message must be at the beginning") is misleading.
_NO_JSON_SCHEMA_WITH_SYSTEM: frozenset[str] = frozenset({"qwen"})


def provider_supports_json_schema_with_system(provider: str) -> bool:
    """True iff the provider accepts ``response_format=json_schema`` *and* a system message together.

    Callers using a system message should branch on this: pass strict
    ``json_schema`` when True, fall back to ``{"type": "json_object"}`` when False.
    Note this only matters when both factors coincide — providers in the
    deny set still accept ``json_schema`` if no system message is present.
    """
    return provider not in _NO_JSON_SCHEMA_WITH_SYSTEM


_LEGACY_EFFORT = {"off": 0, "low": 1024, "high": 8192}


def describe_pdf_input_mode(model: str) -> tuple[str, str | None]:
    """Mirror the PDF dispatch used by step bodies that send PDF input.

    Returns ``(kind, note)`` describing what an AI call would actually receive
    for *model*:

    - ``("PDF", "Gemini, native bytes")``       — Gemini native PDF
    - ``("PDF", "Kimi → server-extracted text")`` — Kimi file-extract path
    - ``("PDF", "Qwen, fileid upload")``         — Qwen models in the PDF prefix list
    - ``("PNG", None)``                          — raster-PNG fallback

    The actual dispatch happens in the per-call helpers (e.g.
    ``xscore/scaffold/scaffold_detect.py``); this function exists so step
    bodies can announce the chosen input mode upfront, the same way
    ``announce_step_model`` mirrors ``make_ai_client`` resolution.
    """
    from eXercise.qwen_input import model_supports_pdf_input
    provider = provider_for_model(model)
    if provider == "gemini":
        return ("PDF", "Gemini, native bytes")
    if provider == "kimi":
        return ("PDF", "Kimi → server-extracted text")
    if provider == "qwen" and model_supports_pdf_input(model):
        return ("PDF", "Qwen, fileid upload")
    return ("PNG", None)


def parse_model_spec(value: str) -> tuple[str, int | None, int | None]:
    """Parse ``"<model>[, <thinking_tokens>][, <max_output_tokens>]"``.

    Returns ``(model, thinking_tokens, max_tokens)``. ``None`` for either budget
    means "caller did not specify — use the code fallback". ``0`` for thinking
    means "explicitly off". Legacy ``off``/``low``/``high`` strings parse to
    ``0``/``1024``/``8192`` for back-compat with the previous two-position
    syntax. Unrecognised tokens are silently skipped.
    """
    parts = [p.strip() for p in value.split(",")]
    model = parts[0]
    nums: list[int] = []
    for p in parts[1:]:
        if not p:
            continue
        low = p.lower()
        if low in _LEGACY_EFFORT:
            nums.append(_LEGACY_EFFORT[low])
        elif p.lstrip("-").isdigit():
            nums.append(int(p))
    thinking = nums[0] if len(nums) >= 1 else None
    max_tokens = nums[1] if len(nums) >= 2 else None
    return model, thinking, max_tokens


def format_model_announcement(
    model: str,
    thinking_tokens: int | None = None,
    max_tokens: int | None = None,
) -> str:
    """Return the canonical 'Model: <name>[, thinking_tokens=N][, max_tokens=N]' string.

    Each budget is included only when not None — callers that want the output
    token count to always appear should substitute their leaf's effective
    fallback for *max_tokens* before calling.
    """
    parts = [model]
    if thinking_tokens is not None:
        parts.append(f"thinking_tokens={thinking_tokens}")
    if max_tokens is not None:
        parts.append(f"max_tokens={max_tokens}")
    return f"Model: {', '.join(parts)}"


def resolve_active_model(
    env_chain: tuple[str, ...] | list[str],
    default: str | None = None,
) -> tuple[str, str, int | None]:
    """Walk *env_chain* (env var names) and return ``(model, provider, thinking_tokens)``.

    Used when a caller needs to know which provider would be used without actually
    building a client (e.g. to decide between the Gemini PDF-upload path and the
    OpenAI-compat path before calling ``make_ai_client``).

    Looks up each env var in order; falls back to *default*, then
    ``AI_DEFAULT_MODEL``, then the module's ``_DEFAULT_MODEL``.
    """
    raw = ""
    for env_name in env_chain:
        raw = os.environ.get(env_name, "").strip()
        if raw:
            break
    if not raw:
        raw = (
            (default or "").strip()
            or os.environ.get("AI_DEFAULT_MODEL", "").strip()
            or _DEFAULT_MODEL
        )
    model, thinking, _ = parse_model_spec(raw)
    return model, provider_for_model(model), thinking


def build_thinking_kwargs(
    provider: str, thinking_tokens: int | None
) -> tuple[bool, dict]:
    """Return ``(use_stream, extra_kwargs)`` for ``client.chat.completions.create()``.

    The caller should pass ``**extra_kwargs`` to ``create()`` and, when
    ``use_stream`` is True, consume the response with
    ``collect_streamed_response()`` instead of reading ``message.content``.

    Mapping (integer thinking budget → provider param)
    --------------------------------------------------
    Gemini  — OpenAI-compat ``reasoning_effort`` accepts {none, low, high} only.
              ``0`` → ``"none"``. ``1..1024`` → ``"low"``. ``>1024`` → ``"high"``.
              ``None`` (caller didn't specify) = provider default (no param);
              streams to show live output.
    Qwen    — ``0`` or ``None`` disables thinking (non-streaming). Any positive
              value enables thinking (forces stream); use
              :func:`build_completion_kwargs` to also pass the integer through
              as Dashscope's ``thinking_budget``.
    Kimi    — ``0`` or ``None`` disables thinking (non-streaming, JSON-friendly).
              Any positive value enables thinking via
              ``extra_body={"thinking": {"type": "enabled"}}`` AND forces
              streaming, mirroring Qwen — non-streaming K2 thinking calls
              blocked the whole reply (often >5 min on long-form scaffold
              calls) and looked like a hang. Older ``moonshot-v1-*`` ids
              ignore the thinking field.
    Grok    — silently ignored; always non-streaming.
    """
    if provider == "gemini":
        if thinking_tokens is None:
            # Provider default — stream to show live output
            return True, {}
        if thinking_tokens == 0:
            return False, {"reasoning_effort": "none"}
        effort = "low" if thinking_tokens <= 1024 else "high"
        return True, {"reasoning_effort": effort}

    if provider == "qwen":
        if thinking_tokens is None or thinking_tokens == 0:
            return False, {"extra_body": {"enable_thinking": False}}
        return True, {"extra_body": {"enable_thinking": True}}

    if provider == "kimi":
        if thinking_tokens is None or thinking_tokens == 0:
            return False, {"extra_body": {"thinking": {"type": "disabled"}}}
        # Thinking enabled → force streaming (matches Qwen). Non-streaming
        # K2 thinking calls block until the entire reply is generated, which
        # for long-form scaffold parsing routinely exceeds the OpenAI SDK's
        # default read timeout and looks like a hang from the caller's side.
        return True, {"extra_body": {"thinking": {"type": "enabled"}}}

    # grok or unknown — no thinking params
    return False, {}


def build_completion_kwargs(
    provider: str,
    thinking_tokens: int | None,
    max_tokens: int | None,
) -> tuple[bool, dict]:
    """Return ``(use_stream, kwargs)`` for ``client.chat.completions.create()``.

    Superset of :func:`build_thinking_kwargs` that also threads:

    * ``max_tokens`` — when not None, becomes the ``max_tokens=`` API param.
    * ``thinking_budget`` for Qwen — when thinking is on, the integer value is
      added inside ``extra_body`` alongside ``enable_thinking``. Other
      providers ignore it.

    Drop-in for ``build_thinking_kwargs``: callers spread the kwargs the same
    way (``client.chat.completions.create(**kwargs)``).
    """
    use_stream, kw = build_thinking_kwargs(provider, thinking_tokens)
    if max_tokens is not None:
        kw = {**kw, "max_tokens": max_tokens}
    if (
        provider == "qwen"
        and thinking_tokens is not None
        and thinking_tokens > 0
    ):
        eb = dict(kw.get("extra_body") or {})
        eb["thinking_budget"] = int(thinking_tokens)
        kw = {**kw, "extra_body": eb}
    return use_stream, kw


# ---------------------------------------------------------------------------
# Tracking proxies — wrap clients so every completion records token usage
# ---------------------------------------------------------------------------

class _UsageTrackingStream:
    """Wraps a streaming completion; records usage + call duration from the final no-choices chunk."""

    def __init__(self, stream: Any, model: str, t0: float | None = None) -> None:
        self._stream = stream
        self._model = model
        self._t0 = t0
        self._recorded = False

    def __iter__(self):
        # Record once when iteration ends (normal exhaustion or early
        # termination via GeneratorExit). Some providers send usage on a
        # trailing no-choices chunk (OpenAI canon); others piggyback it on
        # the final content chunk. Track the latest non-None usage seen and
        # commit it in finally — works for both shapes and never double-records.
        last_usage = None
        try:
            for chunk in self._stream:
                u = getattr(chunk, "usage", None)
                if u is not None:
                    last_usage = u
                yield chunk
        finally:
            if last_usage is not None and self._model and not self._recorded:
                record_usage(
                    self._model,
                    getattr(last_usage, "prompt_tokens", 0) or 0,
                    getattr(last_usage, "completion_tokens", 0) or 0,
                    _extract_reasoning_tokens(last_usage),
                )
                if self._t0 is not None:
                    record_call(self._model, time.perf_counter() - self._t0)
                self._recorded = True

    def __enter__(self) -> "_UsageTrackingStream":
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        if hasattr(self._stream, "__exit__"):
            return self._stream.__exit__(*args)


class _CachingStream:
    """Wraps an already-_UsageTrackingStream-wrapped real stream, accumulating
    ``delta.content`` so the assembled answer text can be written to the
    response cache on full exhaustion.

    Writes only on a clean iteration to ``StopIteration``. Early break,
    ``GeneratorExit``, or a mid-stream exception leave the cache untouched —
    a partially-consumed stream may not represent the model's full reply.
    """

    def __init__(self, stream: Any, key: str, model: str) -> None:
        self._stream = stream
        self._key = key
        self._model = model
        self._parts: list[str] = []
        self._complete = False

    def __iter__(self):
        try:
            for chunk in self._stream:
                if getattr(chunk, "choices", None):
                    delta = chunk.choices[0].delta
                    c = getattr(delta, "content", None)
                    if c:
                        self._parts.append(c)
                yield chunk
            self._complete = True
        finally:
            if self._complete and self._parts:
                from xscore.shared.response_cache import cache_put
                cache_put(self._key, model=self._model, response="".join(self._parts).strip())

    def __enter__(self) -> "_CachingStream":
        if hasattr(self._stream, "__enter__"):
            self._stream.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        if hasattr(self._stream, "__exit__"):
            return self._stream.__exit__(*args)


def _build_oa_cache_hit_response(text: str, *, stream: bool) -> Any:
    """Duck-type a cache-hit return value matching the OpenAI-compat response shape.

    Non-streaming: SimpleNamespace mimicking ``ChatCompletion`` enough that
    ``resp.choices[0].message.content`` works. ``usage=None`` so the wrapper
    skips ``record_usage`` (cached calls don't bill tokens).

    Streaming: a single-chunk iterator yielding the cached text via
    ``delta.content``. ``collect_streamed_response`` reads exactly that.
    """
    from types import SimpleNamespace

    if stream:
        def _gen():
            yield SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content=text, reasoning_content=None),
                    finish_reason="stop",
                )],
                usage=None,
            )
        return _gen()

    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content=text, reasoning_content=""),
            finish_reason="stop",
        )],
        usage=None,
    )


def _build_gemini_cache_hit_response(text: str) -> Any:
    """Duck-type a cache-hit return value matching the Gemini native response shape.

    ``split_gemini_response`` (defined below) walks
    ``resp.candidates[*].content.parts[*]`` and falls back to ``resp.text``.
    Both populated. ``usage_metadata=None`` so the wrapper skips
    ``record_usage`` for cached calls.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text=text, thought=False)]),
        )],
        text=text,
        usage_metadata=None,
    )


class _TrackedCompletions:
    def __init__(
        self,
        completions: Any,
        model: str,
        deterministic: bool = True,
        should_cache: bool = False,
    ) -> None:
        self._c = completions
        self._model = model
        self._deterministic = deterministic
        self._should_cache = should_cache

    def create(self, *args: Any, **kwargs: Any) -> Any:
        # kimi-k2.x rejects any temperature other than its fixed default
        # (1.0 thinking / 0.6 non-thinking) with a 400 error, so skip the
        # determinism injection for those ids. Older moonshot-v1-* accept
        # temperature normally.
        inject_determinism = self._deterministic and not self._model.startswith("kimi-k2")
        # Gemini's OpenAI-compat endpoint rejects the ``seed`` parameter with
        # a 400 INVALID_ARGUMENT error — skip seed injection for those models.
        inject_seed = inject_determinism and not self._model.lower().startswith("gemini")
        if inject_determinism:
            if "temperature" not in kwargs:
                t = _read_default_temperature()
                if t is not None:
                    kwargs["temperature"] = t
            if inject_seed and "seed" not in kwargs:
                s = _read_default_seed()
                if s is not None:
                    kwargs["seed"] = s
        is_stream = kwargs.get("stream", False)
        if is_stream:
            # OpenAI-compat providers (DashScope/Qwen, Gemini OpenAI-compat,
            # OpenAI, xAI) only emit the final usage chunk when include_usage
            # is opted into. Without it, _UsageTrackingStream sees no usage
            # and silently records nothing — which is how MARKING_MODEL with
            # thinking>0 lost its tokens from step 32's cost report.
            opts = dict(kwargs.get("stream_options") or {})
            opts.setdefault("include_usage", True)
            kwargs["stream_options"] = opts

        # Response cache (opt-in via client._should_cache, set by the factory
        # when the user passed ``should_cache=reuse_cache_enabled(ctx)``).
        # Computed AFTER determinism injection so the key reflects what the
        # model actually sees.
        cache_key_str: str | None = None
        if self._should_cache:
            from xscore.shared.response_cache import (
                cache_get, derive_oa_cache_key,
            )
            cache_key_str = derive_oa_cache_key(self._model, kwargs.get("messages") or [])
            if cache_key_str is not None:
                hit = cache_get(cache_key_str)
                if hit and isinstance(hit.get("response"), str) and hit["response"]:
                    return _build_oa_cache_hit_response(hit["response"], stream=is_stream)

        t0 = time.perf_counter()
        resp = self._c.create(*args, **kwargs)
        if is_stream:
            wrapped = _UsageTrackingStream(resp, self._model, t0)
            if cache_key_str is not None:
                return _CachingStream(wrapped, cache_key_str, self._model)
            return wrapped
        u = getattr(resp, "usage", None)
        if u:
            record_usage(
                self._model,
                getattr(u, "prompt_tokens", 0) or 0,
                getattr(u, "completion_tokens", 0) or 0,
                _extract_reasoning_tokens(u),
            )
            record_call(self._model, time.perf_counter() - t0)
        if cache_key_str is not None:
            from xscore.shared.response_cache import cache_put
            text = (resp.choices[0].message.content or "") if resp.choices else ""
            if text:
                cache_put(cache_key_str, model=self._model, response=text)
        return resp

    def __getattr__(self, name: str) -> Any:
        return getattr(self._c, name)


class _TrackedChat:
    def __init__(
        self,
        chat: Any,
        model: str,
        deterministic: bool = True,
        should_cache: bool = False,
    ) -> None:
        self._chat = chat
        self.completions = _TrackedCompletions(
            chat.completions, model, deterministic=deterministic, should_cache=should_cache,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._chat, name)


class _TrackedOpenAIClient:
    """Thin proxy over OpenAI that records token usage for every completion.

    When ``deterministic=True`` (the default) and a per-call ``temperature`` /
    ``seed`` is not supplied, ``ALL_AI_TEMPERATURE`` / ``ALL_AI_SEED`` env vars are
    injected into ``chat.completions.create``. Pass ``deterministic=False`` to
    skip injection entirely (use for ad-hoc creative-sampling calls).

    When ``should_cache=True``, every ``chat.completions.create`` consults
    the response cache (see :mod:`xscore.shared.response_cache`) before
    hitting the API, and writes the response on miss. ``kimi_pdf_text`` also
    reads ``client._should_cache`` to gate its own file-extract caching.
    """

    def __init__(
        self,
        client: Any,
        model: str,
        deterministic: bool = True,
        should_cache: bool = False,
    ) -> None:
        self._client = client
        self._should_cache = should_cache
        self.chat = _TrackedChat(
            client.chat, model, deterministic=deterministic, should_cache=should_cache,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class _TrackedGeminiModels:
    """Wraps ``client.models`` so every ``generate_content`` records token usage.

    The recorded ``output_tokens`` is ``candidates_token_count + thoughts_token_count``
    so it matches the OpenAI-compat ``usage.completion_tokens`` semantic (visible +
    thinking, the number Gemini actually bills). ``thoughts_token_count`` is also
    recorded separately as ``thinking`` for the cost-report breakdown.
    """

    def __init__(
        self,
        models: Any,
        deterministic: bool = True,
        should_cache: bool = False,
    ) -> None:
        self._m = models
        self._deterministic = deterministic
        self._should_cache = should_cache

    def _apply_deterministic(self, kwargs: dict) -> None:
        """Inject ALL_AI_TEMPERATURE / ALL_AI_SEED into the ``config`` kwarg if not set.

        Native Gemini uses ``GenerateContentConfig`` (a Pydantic model) on the
        ``config`` keyword. If callers pass no config, build one with just
        temperature/seed; if they pass one, set fields only when currently None.
        Failures are swallowed — determinism is best-effort, not load-bearing.
        """
        if not self._deterministic:
            return
        t = _read_default_temperature()
        s = _read_default_seed()
        if t is None and s is None:
            return
        cfg = kwargs.get("config")
        try:
            if cfg is not None:
                if t is not None and getattr(cfg, "temperature", None) is None:
                    cfg.temperature = t
                if s is not None and getattr(cfg, "seed", None) is None:
                    cfg.seed = s
            else:
                from google.genai import types as gtypes  # type: ignore[import-not-found]
                kwargs["config"] = gtypes.GenerateContentConfig(
                    temperature=t,
                    seed=s,
                )
        except Exception:
            pass

    def generate_content(self, *args: Any, **kwargs: Any) -> Any:
        self._apply_deterministic(kwargs)
        model = kwargs.get("model") or (args[0] if args else "unknown")

        cache_key_str: str | None = None
        if self._should_cache:
            from xscore.shared.response_cache import (
                cache_get, derive_gemini_cache_key,
            )
            cache_key_str = derive_gemini_cache_key(
                str(model), kwargs.get("contents"), kwargs.get("config"),
            )
            if cache_key_str is not None:
                hit = cache_get(cache_key_str)
                if hit and isinstance(hit.get("response"), str) and hit["response"]:
                    return _build_gemini_cache_hit_response(hit["response"])

        t0 = time.perf_counter()
        resp = self._m.generate_content(*args, **kwargs)
        um = getattr(resp, "usage_metadata", None)
        if um:
            # candidates_token_count is *visible* output only; thoughts_token_count
            # is the thinking portion. Sum them so output_tokens matches the
            # OpenAI-compat semantic (and what Gemini actually bills).
            visible  = getattr(um, "candidates_token_count", 0) or 0
            thoughts = getattr(um, "thoughts_token_count",   0) or 0
            record_usage(
                str(model),
                getattr(um, "prompt_token_count", 0) or 0,
                visible + thoughts,
                thoughts,
            )
            record_call(str(model), time.perf_counter() - t0)
        if cache_key_str is not None:
            from xscore.shared.response_cache import cache_put
            text, _thinking = split_gemini_response(resp)
            if text:
                cache_put(cache_key_str, model=str(model), response=text)
        return resp

    def __getattr__(self, name: str) -> Any:
        return getattr(self._m, name)


class _TrackedGeminiClient:
    """Thin proxy over google.genai.Client that records token usage.

    See :class:`_TrackedOpenAIClient` for the determinism + cache contracts
    — same semantics, different transport (``GenerateContentConfig`` instead
    of ``chat.completions.create`` kwargs).
    """

    def __init__(
        self,
        client: Any,
        deterministic: bool = True,
        should_cache: bool = False,
    ) -> None:
        self._client = client
        self._should_cache = should_cache
        self.models = _TrackedGeminiModels(
            client.models, deterministic=deterministic, should_cache=should_cache,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


_TIMEOUT_TIERS: dict[str, tuple[str, float]] = {
    "quick":    ("AI_TIMEOUT_QUICK_S", 60.0),
    "standard": ("AI_TIMEOUT_STANDARD_S", 180.0),
    "long":     ("AI_TIMEOUT_LONG_S", 360.0),
}


def make_request_timeout(tier: str) -> "Any | None":
    """Build an ``httpx.Timeout`` for one of the three call tiers.

    quick    → ``AI_TIMEOUT_QUICK_S``    (default 60 s)  — single-decision calls
    standard → ``AI_TIMEOUT_STANDARD_S`` (default 180 s) — vision matching, medium output
    long     → ``AI_TIMEOUT_LONG_S``     (default 360 s) — heavy generation, marking

    The read timeout is the **inter-chunk** timeout for streaming responses,
    not the total request duration. A streamed call that takes longer than
    the tier value is fine as long as no single gap between chunks exceeds it.

    Returns ``None`` when the env value is ≤ 0 (debug escape hatch — callers
    that spread ``{"timeout": result} if result else {}`` then send no
    per-call timeout, falling back to the OpenAI SDK default).
    """
    import httpx  # noqa: PLC0415

    try:
        env_var, default_s = _TIMEOUT_TIERS[tier]
    except KeyError as exc:
        raise ValueError(
            f"unknown tier {tier!r}; expected one of {sorted(_TIMEOUT_TIERS)}"
        ) from exc
    try:
        s = float(os.environ.get(env_var, str(default_s)))
    except ValueError:
        s = default_s
    if s <= 0:
        return None
    return httpx.Timeout(connect=30.0, read=s, write=30.0, pool=30.0)


def make_ai_client(
    *,
    model_env: str = "AI_DEFAULT_MODEL",
    legacy_model_env: str = "XAI_MODEL",
    default_model: str | None = None,
    deterministic: bool = True,
    should_cache: bool = False,
) -> tuple[Any, str, str, int | None, int | None] | None:
    """Return ``(client, model, provider, thinking_tokens, max_tokens)`` or ``None``.

    Returns ``None`` when the required API key is missing.

    Parameters
    ----------
    model_env:
        Primary env var for the model (e.g. ``"NL_MODEL"``). May contain a
        ``, <thinking_tokens>[, <max_output_tokens>]`` suffix.
    legacy_model_env:
        Fallback env var if *model_env* is unset (e.g. ``"AI_DEFAULT_MODEL"``).
    default_model:
        Model string (optionally with budget suffixes) when neither env var is
        set. Defaults to ``AI_DEFAULT_MODEL`` → ``_DEFAULT_MODEL``.
    deterministic:
        When True (default), every call through the returned client gets
        ``temperature`` and ``seed`` injected from ``ALL_AI_TEMPERATURE`` /
        ``ALL_AI_SEED`` env vars unless the caller supplied them. Pass False to
        disable injection (rare — use only when you actively want sampling).
    should_cache:
        When True, every ``chat.completions.create`` consults the response
        cache (see :mod:`xscore.shared.response_cache`); ``kimi_pdf_text``
        also reads ``client._should_cache`` to gate its own file-extract
        caching. Resolve via ``reuse_cache_enabled(ctx)`` at the call site.

    Returned ``thinking_tokens`` / ``max_tokens`` are ``None`` when the env
    string didn't specify them — callers should fall back to their own default.
    """
    try:
        from openai import OpenAI
    except ImportError:
        return None

    raw = (
        os.environ.get(model_env, "").strip()
        or os.environ.get(legacy_model_env, "").strip()
        or default_model
        or os.environ.get("AI_DEFAULT_MODEL", "").strip()
        or _DEFAULT_MODEL
    )

    model, thinking_tokens, max_tokens = parse_model_spec(raw)
    provider = provider_for_model(model)
    pdef = next((p for p in _PROVIDER_REGISTRY if p.name == provider), _PROVIDER_REGISTRY[0])

    api_key = os.environ.get(pdef.api_key_env, "").strip()
    if not api_key and pdef.name == "gemini":
        api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        return None

    # Kimi accepts KIMI_BASE_URL as a runtime override (.cn vs .ai endpoint).
    base_url = pdef.base_url
    if pdef.name == "kimi":
        base_url = os.environ.get("KIMI_BASE_URL", "").strip() or base_url

    try:
        client = OpenAI(api_key=api_key, base_url=base_url)
    except Exception:
        return None

    return (
        _TrackedOpenAIClient(client, model, deterministic=deterministic, should_cache=should_cache),
        model,
        provider,
        thinking_tokens,
        max_tokens,
    )


def strip_json_fences(raw: str) -> str:
    """Remove markdown code fences that some models add despite being told not to.

    Handles ```json ... ```, ``` ... ```, and leading/trailing whitespace.
    Falls back to extracting the first balanced { ... } block when prose surrounds the JSON.
    """
    import re
    s = raw.strip()
    fence = re.match(r"^```(?:json)?\s*([\s\S]*?)```\s*$", s)
    if fence:
        return fence.group(1).strip()
    # Stack-walk to find the first balanced { … } so we don't greedily span
    # across multiple top-level JSON objects in the same response.
    start = s.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i, ch in enumerate(s[start:], start):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_string:
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start : i + 1]
    return s


def get_api_key_env_name(provider: str | None = None) -> str:
    """Return the env var name for the given provider's API key.

    If *provider* is None, returns the key env for the default model's provider.
    """
    p = provider if provider else provider_for_model(
        os.environ.get("AI_DEFAULT_MODEL", "").strip() or _DEFAULT_MODEL
    )
    pdef = next((pd for pd in _PROVIDER_REGISTRY if pd.name == p), _PROVIDER_REGISTRY[0])
    return pdef.api_key_env


def collect_streamed_response(
    stream: Any, *, thinking_out: list[str] | None = None
) -> str:
    """Consume a streaming chat completion and return the answer text.

    Accumulates ``delta.content`` (the final answer). When *thinking_out* is
    a list, ``delta.reasoning_content`` chunks (the thinking/scratchpad) are
    appended to it; otherwise they are discarded. Works for any provider
    that returns a streaming completion; specifically designed for Qwen's
    thinking-mode responses.
    """
    parts: list[str] = []
    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta.content:
            parts.append(delta.content)
        if thinking_out is not None:
            r = getattr(delta, "reasoning_content", None)
            if r:
                thinking_out.append(r)
    return "".join(parts).strip()


def split_gemini_response(resp: Any) -> tuple[str, str]:
    """Return ``(answer_text, thinking_text)`` from a native Gemini response.

    Walks ``resp.candidates[*].content.parts[*]`` separating ``part.thought``
    parts from answer parts. Falls back to ``resp.text`` for ``answer_text``
    when no parts are seen.
    """
    thinking_parts: list[str] = []
    answer_parts: list[str] = []
    for candidate in (getattr(resp, "candidates", None) or []):
        for part in getattr(getattr(candidate, "content", None), "parts", None) or []:
            text = getattr(part, "text", None) or ""
            if getattr(part, "thought", False):
                thinking_parts.append(text)
            else:
                answer_parts.append(text)
    answer = "".join(answer_parts) or (getattr(resp, "text", "") or "")
    return answer, "".join(thinking_parts)


def build_gemini_thinking_config(thinking_tokens: int | None) -> Any:
    """Return a ``google.genai.types.ThinkingConfig`` for *thinking_tokens*.

    Native Gemini SDK accepts arbitrary integer ``thinking_budget`` values, so
    this is a direct pass-through (unlike :func:`build_thinking_kwargs` which
    has to bucket the integer for the OpenAI-compat ``reasoning_effort`` enum).

    * ``None`` — provider default (``include_thoughts=True``, no explicit budget).
    * ``0``    — thinking off (``thinking_budget=0``, ``include_thoughts=False``).
    * ``N>0``  — explicit budget of ``N`` tokens, thoughts included.
    """
    from google.genai import types as gai_types  # noqa: PLC0415
    if thinking_tokens is None:
        return gai_types.ThinkingConfig(include_thoughts=True)
    if thinking_tokens == 0:
        return gai_types.ThinkingConfig(thinking_budget=0, include_thoughts=False)
    return gai_types.ThinkingConfig(
        thinking_budget=thinking_tokens, include_thoughts=True
    )


def make_gemini_native_client(
    *,
    deterministic: bool = True,
    should_cache: bool = False,
) -> Any:
    """Return a ``google.genai.Client`` for the Gemini native SDK, or ``None`` if no API key.

    Reads ``GEMINI_API_KEY`` with ``GOOGLE_API_KEY`` as fallback — the same key
    resolution used by every pipeline call site that needs the native Gemini SDK
    (scaffold parsing, multi-page PDF upload, etc.).

    Returns ``None`` rather than raising so callers can decide whether the key
    is required for their specific step.

    When ``deterministic`` is True (default), ``ALL_AI_TEMPERATURE`` and ``ALL_AI_SEED``
    are injected into every ``generate_content`` call's ``GenerateContentConfig``
    unless the caller already set those fields.

    When ``should_cache=True``, every ``models.generate_content`` consults the
    response cache (see :mod:`xscore.shared.response_cache`). Resolve via
    ``reuse_cache_enabled(ctx)`` at the call site.

    A request-level HTTP timeout is configured from ``GEMINI_REQUEST_TIMEOUT_S``
    (default 300 s). This caps any single ``generate_content`` call so a stalled
    connection surfaces as a retryable ``httpx.ReadTimeout`` instead of blocking
    a worker thread forever — important for the parallel mark-scheme parser,
    where a hung worker would otherwise stall ``ThreadPoolExecutor.map``.
    """
    try:
        from google import genai as gai
        from google.genai import types as gai_types  # noqa: PLC0415
    except ImportError:
        return None
    api_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
    if not api_key:
        return None
    timeout_s = float(os.environ.get("GEMINI_REQUEST_TIMEOUT_S", "300"))
    http_options = gai_types.HttpOptions(timeout=int(timeout_s * 1000))  # ms
    return _TrackedGeminiClient(
        gai.Client(api_key=api_key, http_options=http_options),
        deterministic=deterministic,
        should_cache=should_cache,
    )


_GEMINI_INLINE_PDF_LIMIT = 18 * 1024 * 1024  # 18 MB; Gemini's hard inline cap is ~20 MB


def gemini_pdf_part(client: Any, path: "Path", *, label: str = "pdf") -> Any:
    """Return a Gemini ``Part`` for the PDF at *path*.

    Inlines the bytes via ``Part.from_bytes`` for files ≤ 18 MB (the common case
    in this repo — exam pages, mark scheme splits, student rosters all fit).
    Falls back to ``client.files.upload`` + ``Part.from_uri`` for larger PDFs.

    Replaces the per-caller upload+poll dance that used to be scattered
    across scaffold_gemini, ai_mark, load_student_list, mcq_ai, and
    difficulty_ranking. The Files API path is preserved here for the rare
    >18 MB case (e.g. duplex student scans). For inline calls there is nothing
    to clean up afterwards; for the upload fallback, files auto-expire after
    48 h via Gemini policy.
    """
    from pathlib import Path
    from google.genai import types as gai_types

    if not isinstance(path, Path):
        path = Path(path)
    data = path.read_bytes()
    if len(data) <= _GEMINI_INLINE_PDF_LIMIT:
        return gai_types.Part.from_bytes(data=data, mime_type="application/pdf")

    interval = float(os.environ.get("GEMINI_UPLOAD_POLL_S", "3"))
    timeout = float(os.environ.get("GEMINI_UPLOAD_TIMEOUT_S", "360"))
    f = client.files.upload(file=path)
    deadline = time.monotonic() + timeout
    while getattr(f.state, "name", str(f.state)) == "PROCESSING":
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Gemini file upload timed out ({label}): {f.name}")
        time.sleep(interval)
        f = client.files.get(name=f.name)
    state = getattr(f.state, "name", str(f.state))
    if state == "FAILED":
        raise RuntimeError(f"Gemini file processing failed ({label}): {f.name}")
    return gai_types.Part.from_uri(file_uri=f.uri, mime_type="application/pdf")


def kimi_pdf_text(client: Any, path: "Path", *, label: str = "pdf") -> str:
    """Upload *path* to Moonshot's file-extract endpoint and return the extracted text.

    Mirrors :func:`gemini_pdf_part` for Kimi/Moonshot models, but Moonshot does
    server-side PDF parsing and returns plain text — so callers should inject
    the result as a system message rather than as a multimodal part.

    Files count against the per-account quota (1000 files / 10 GB per Moonshot
    docs); the helper deletes the upload before returning so per-call cost is
    bounded. Cleanup failure is silent (best-effort).

    *client* must be an OpenAI-compatible client pointing at Moonshot
    (e.g. the one returned by :func:`make_ai_client` for a kimi-* model).
    """
    from pathlib import Path
    if not isinstance(path, Path):
        path = Path(path)

    # Opt-in cache: same flag as the chat.completions wrapper. Key folds in
    # the PDF bytes (the only thing that can change the extracted text);
    # *label* is included as user_prompt so two callers extracting the same
    # PDF for different purposes don't collide.
    should_cache = getattr(client, "_should_cache", False)
    cache_key_str: str | None = None
    if should_cache:
        from xscore.shared.response_cache import cache_get, cache_key as _cache_key
        cache_key_str = _cache_key(
            model="kimi-file-extract",
            system_prompt="",
            user_prompt=label,
            image_bytes=path.read_bytes(),
        )
        hit = cache_get(cache_key_str)
        if hit and isinstance(hit.get("response"), str):
            return hit["response"]

    file_obj = client.files.create(file=path, purpose="file-extract")
    try:
        # Guard ``.text``: an SDK upgrade or a gateway error could return an
        # object without that attribute, and a bare attribute access would
        # raise AttributeError mid-pipeline. Returning empty text lets the
        # caller fall back to the rasterized-image path instead of crashing.
        content = client.files.content(file_obj.id)
        text = getattr(content, "text", "") or ""
        if cache_key_str is not None and text:
            from xscore.shared.response_cache import cache_put
            cache_put(cache_key_str, model="kimi-file-extract", response=text)
        return text
    finally:
        try:
            client.files.delete(file_obj.id)
        except Exception:
            pass


def is_transient_error(exc: BaseException) -> bool:
    """Return True if exc is a transient server error worth retrying.

    Covers HTTP 503 from any supported provider SDK, and connection drops
    (httpx.RemoteProtocolError — "Server disconnected without sending a response.").
    """
    try:
        from openai import APIStatusError
        if isinstance(exc, APIStatusError) and exc.status_code == 503:
            return True
    except ImportError:
        pass
    try:
        from google.genai.errors import APIError
        if isinstance(exc, APIError) and exc.code == 503:
            return True
    except ImportError:
        pass
    try:
        from httpx import RemoteProtocolError
        if isinstance(exc, RemoteProtocolError):
            return True
    except ImportError:
        pass
    return False


def print_streamed_response(
    stream: Any,
    *,
    print_thinking: bool = True,
    stream_thinking: bool = True,
    print_content: bool = True,
    indent: str = "  ",
    thinking_out: list | None = None,
    finish_reason_out: list[str] | None = None,
) -> str:
    """Consume a streaming chat completion, print thinking + content live, return content.

    Thinking (``delta.reasoning_content``) is wrapped in ``[thinking]`` /
    ``[/thinking]`` blocks.  Content (``delta.content``) is printed as-is.
    Only ``delta.content`` is accumulated and returned.

    *print_thinking* controls whether the ``[thinking]`` markers are shown.
    *stream_thinking* controls whether the actual thinking token text is streamed;
    when False the markers still appear but the content is silent.
    If *thinking_out* is a list, thinking text is appended to it regardless.
    If *finish_reason_out* is a list, each non-empty ``choice.finish_reason``
    seen on a chunk is appended to it (the last entry is the final reason).
    """
    content_parts: list[str] = []
    in_thinking = False
    for chunk in stream:
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        if finish_reason_out is not None and choice.finish_reason:
            finish_reason_out.append(choice.finish_reason)
        delta = choice.delta

        thinking_text = getattr(delta, "reasoning_content", None) or ""
        content_text = delta.content or ""

        if thinking_text:
            if thinking_out is not None:
                thinking_out.append(thinking_text)
            if print_thinking:
                if not in_thinking:
                    print(f"\n{indent}[thinking]", flush=True)
                    in_thinking = True
                if stream_thinking:
                    print(thinking_text, end="", flush=True)

        if content_text:
            if in_thinking:
                print(f"\n{indent}[/thinking]", flush=True)
                in_thinking = False
            if print_content:
                print(content_text, end="", flush=True)
            content_parts.append(content_text)

    if in_thinking:
        print(f"\n{indent}[/thinking]", flush=True)
    if print_content and content_parts:
        print()  # trailing newline after content
    return "".join(content_parts).strip()
