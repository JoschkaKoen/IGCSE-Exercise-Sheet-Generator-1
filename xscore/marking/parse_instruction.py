"""Translate a natural-language grading prompt into a structured TaskInstruction.

Uses a text-only Gemini call (PARSE_PROMPT_MODEL) so this step is fast and cheap.
"""

from __future__ import annotations

import os
import time

from .ai_helpers import parse_json_safe
from xscore.config import GEMINI_MAX_OUTPUT_TOKENS
from xscore.prompts.loader import load_prompt
from xscore.shared.models import StudentFilter, TaskInstruction
from xscore.shared.terminal_ui import info_line, warn_line

_DEFAULT_MODEL = "gemini-2.5-flash"  # also set as INTERPRET_PROMPT_MODEL in default.env


def _read_model_config() -> tuple[str, int | None, int | None]:
    from eXercise.ai_client import parse_model_spec
    raw = os.getenv("INTERPRET_PROMPT_MODEL") or os.getenv("AI_DEFAULT_MODEL") or _DEFAULT_MODEL
    return parse_model_spec(raw)


def _call_text(user_message: str, out: dict | None = None) -> str:
    """Make a text-only call (any provider) and return the raw response string.

    Routes by model name: Gemini → native SDK; Qwen/Grok → OpenAI-compat.

    When *out* is supplied, populates it with ``model``, ``system``, ``user``,
    ``raw``, ``thinking`` so the caller can persist the prompt+response to disk
    once an artifact dir is available.
    """
    model_name, thinking_tokens, max_tokens = _read_model_config()
    if out is not None:
        out["model"]    = model_name
        out["system"]   = _SYSTEM_PROMPT
        out["user"]     = user_message
        out["raw"]      = ""
        out["thinking"] = ""

    from eXercise.api_retry import retry_api_call

    if model_name.startswith("gemini"):
        try:
            from google.genai import types as gai_types
        except ImportError:
            raise RuntimeError("google-genai not installed; run: pip install google-genai")
        from eXercise.ai_client import (
            build_gemini_thinking_config,
            make_gemini_native_client,
            split_gemini_response,
        )
        client = make_gemini_native_client()
        if client is None:
            raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")
        gen_config_kwargs: dict = {
            "max_output_tokens": max_tokens or GEMINI_MAX_OUTPUT_TOKENS,
            "response_mime_type": "application/json",
        }
        if thinking_tokens is not None:
            gen_config_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)

        def _do_gemini() -> tuple[str, str]:
            _resp = client.models.generate_content(
                model=model_name,
                contents=user_message,
                config=gai_types.GenerateContentConfig(
                    system_instruction=_SYSTEM_PROMPT,
                    **gen_config_kwargs,
                ),
            )
            return split_gemini_response(_resp)

        raw, thinking = retry_api_call(_do_gemini, label="Parse instruction")
        if out is not None:
            out["raw"], out["thinking"] = raw, thinking
        return raw

    # OpenAI-compat path (Qwen, Grok, …)
    from eXercise.ai_client import (
        build_completion_kwargs,
        collect_streamed_response,
        make_ai_client,
        provider_for_model,
    )
    result = make_ai_client(model_env="INTERPRET_PROMPT_MODEL")
    if result is None:
        raise RuntimeError(
            f"INTERPRET_PROMPT_MODEL={model_name} requires the API key for "
            f"provider '{provider_for_model(model_name)}' in .env"
        )
    client, _, provider, _, _ = result
    use_stream, kw = build_completion_kwargs(provider, thinking_tokens, max_tokens)
    from eXercise.ai_client import make_request_timeout  # noqa: PLC0415
    _timeout = make_request_timeout("quick")
    _timeout_kw: dict = {"timeout": _timeout} if _timeout is not None else {}
    msgs = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    if use_stream:
        def _do_stream() -> tuple[str, str]:
            # Some providers reject response_format with stream=True — omit it
            # on the streaming branch; the prompt itself enforces JSON output.
            # Stream consumed inside the closure so a mid-stream failure retries.
            _th: list[str] = []
            _stream = client.chat.completions.create(
                model=model_name, messages=msgs, stream=True, **kw, **_timeout_kw,
            )
            return collect_streamed_response(_stream, thinking_out=_th), "".join(_th)

        raw, thinking = retry_api_call(_do_stream, label="Parse instruction (stream)")
        if out is not None:
            out["raw"], out["thinking"] = raw, thinking
        return raw

    def _do_json() -> tuple[str, str]:
        _resp = client.chat.completions.create(
            model=model_name, messages=msgs,
            response_format={"type": "json_object"}, **kw, **_timeout_kw,
        )
        return (
            _resp.choices[0].message.content or "",
            getattr(_resp.choices[0].message, "reasoning_content", "") or "",
        )

    def _do_plain() -> tuple[str, str]:
        _resp = client.chat.completions.create(
            model=model_name, messages=msgs, **kw, **_timeout_kw,
        )
        return (
            _resp.choices[0].message.content or "",
            getattr(_resp.choices[0].message, "reasoning_content", "") or "",
        )

    try:
        raw, thinking = retry_api_call(_do_json, label="Parse instruction (json)")
    except Exception:
        # Provider may reject response_format=json_object — fall through to plain.
        raw, thinking = retry_api_call(_do_plain, label="Parse instruction (plain)")
    if out is not None:
        out["raw"], out["thinking"] = raw, thinking
    return raw


_SYSTEM_PROMPT = load_prompt("parse_grading_instructions")[1]


def parse_prompt(
    prompt: str,
    client: object | None = None,  # ignored — kept for backward compatibility
    *,
    out: dict | None = None,
) -> TaskInstruction:
    """Parse *prompt* into a ``TaskInstruction`` via a Gemini text call.

    Uses PARSE_PROMPT_MODEL (default: gemini-2.5-flash, low).

    Raises ``RuntimeError`` if the AI call fails, returns nothing, or returns
    unparseable JSON — the caller should surface the error to the user and let
    them re-phrase the prompt. There is no heuristic fallback by design.

    When *out* is supplied (a mutable dict), it is populated with debug fields
    (``model``, ``system``, ``user``, ``raw``, ``thinking``) so the caller can
    persist the prompt+response to disk once an artifact dir is available.
    """
    raw = _call_text(prompt, out=out)

    if not raw.strip():
        raise RuntimeError(
            "Empty AI response from parse_grading_instructions — please re-phrase the prompt."
        )

    data = parse_json_safe(raw)
    if data is None:
        raise RuntimeError(
            "Could not parse AI response from parse_grading_instructions — please re-phrase the prompt."
        )

    sf_raw = data.get("student_filter") or {}
    if not isinstance(sf_raw, dict):
        sf_raw = {}
    raw_names = sf_raw.get("names")
    if not isinstance(raw_names, list):
        raw_names = []
    raw_n = sf_raw.get("n")
    try:
        n_students = int(raw_n) if raw_n is not None and raw_n != "" else 0
    except (TypeError, ValueError):
        n_students = 0

    mode_raw = str(sf_raw.get("mode") or "all").strip().lower().replace("-", "_").replace(" ", "_")
    if mode_raw not in ("all", "specific", "first_n"):
        warn_line(f"Unknown student_filter.mode {sf_raw.get('mode')!r} — using 'all'.")
        mode_raw = "all"
    names = [str(x) for x in raw_names if x is not None]

    if mode_raw == "specific" and not names:
        warn_line("student_filter specific had empty names — using 'all'.")
        mode_raw = "all"
    if mode_raw == "first_n" and n_students <= 0:
        warn_line("student_filter first_n had invalid n — using 'all'.")
        mode_raw = "all"
        n_students = 0

    student_filter = StudentFilter(
        mode=mode_raw,
        names=names if mode_raw == "specific" else [],
        n=n_students if mode_raw == "first_n" else 0,
    )

    raw_hint = data.get("folder_hint")
    folder_hint = str(raw_hint).strip() if raw_hint not in (None, "") else None
    raw_fp = data.get("folder_path")
    folder_path = str(raw_fp).strip() if raw_fp not in (None, "") else None

    force_clean_scan = bool(data.get("force_clean_scan", False))
    no_report = bool(data.get("no_report", False))
    raw_from_step = data.get("from_step")
    try:
        from_step = int(raw_from_step) if raw_from_step not in (None, "") else None
    except (TypeError, ValueError):
        from_step = None
    raw_stop_after = data.get("stop_after")
    try:
        stop_after = int(raw_stop_after) if raw_stop_after not in (None, "") else None
    except (TypeError, ValueError):
        stop_after = None
    # AI-set value takes priority; default to False when the AI omits the field.
    reuse_cache = bool(data.get("reuse_cache", False))

    if "curved_grade_override" in data:
        raw_curve = data.get("curved_grade_override")
        try:
            curved_grade_override = (
                int(raw_curve) if raw_curve not in (None, "") else None
            )
        except (TypeError, ValueError):
            curved_grade_override = None
        if curved_grade_override is not None and not (0 <= curved_grade_override <= 100):
            info_line(
                f"AI returned out-of-range curved_grade_override={curved_grade_override} — ignoring"
            )
            curved_grade_override = None
    else:
        curved_grade_override = None

    if "curved_grade_visible" in data:
        raw_vis = data.get("curved_grade_visible")
        if raw_vis is None:
            curved_grade_visible: bool | None = None
        else:
            curved_grade_visible = bool(raw_vis)
    else:
        curved_grade_visible = None

    _VALID_TASK_TYPES = {"count_marks", "check_mc", "check_answers"}
    raw_task = data.get("task_type", "check_answers")
    if raw_task not in _VALID_TASK_TYPES:
        info_line(f"AI returned unknown task_type {raw_task!r} — defaulting to 'check_answers'")
        raw_task = "check_answers"

    return TaskInstruction(
        task_type=raw_task,
        student_filter=student_filter,
        folder_hint=folder_hint,
        folder_path=folder_path,
        force_clean_scan=force_clean_scan,
        no_report=no_report,
        from_step=from_step,
        stop_after=stop_after,
        reuse_cache=reuse_cache,
        curved_grade_override=curved_grade_override,
        curved_grade_visible=curved_grade_visible,
    )
