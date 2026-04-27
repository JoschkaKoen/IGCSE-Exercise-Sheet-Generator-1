"""Translate a natural-language grading prompt into a structured TaskInstruction.

Uses a text-only Gemini call (PARSE_PROMPT_MODEL) so this step is fast and cheap.
"""

from __future__ import annotations

import os
import re
import time

from .ai_helpers import parse_json_safe
from xscore.config import GEMINI_MAX_OUTPUT_TOKENS, PIPELINE_DEFAULT_DPI
from xscore.prompts.loader import load_prompt
from xscore.shared.models import StudentFilter, TaskInstruction
from xscore.shared.terminal_ui import info_line, warn_line

# Matches quoted paths ("…" or '…') first, then bare paths starting with / or ~.
# Trailing sentence punctuation is excluded from bare paths.
_PATH_RE = re.compile(
    r'"((?:/|~)[^"]+)"'                   # double-quoted path
    r"|'((?:/|~)[^']+)'"                  # single-quoted path
    r"|(?<!\S)((?:/|~)[^\s,;.!?]+)"       # bare path (no trailing punctuation)
)

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
                model=model_name, messages=msgs, stream=True, **kw,
            )
            return collect_streamed_response(_stream, thinking_out=_th), "".join(_th)

        raw, thinking = retry_api_call(_do_stream, label="Parse instruction (stream)")
        if out is not None:
            out["raw"], out["thinking"] = raw, thinking
        return raw

    def _do_json() -> tuple[str, str]:
        _resp = client.chat.completions.create(
            model=model_name, messages=msgs,
            response_format={"type": "json_object"}, **kw,
        )
        return (
            _resp.choices[0].message.content or "",
            getattr(_resp.choices[0].message, "reasoning_content", "") or "",
        )

    def _do_plain() -> tuple[str, str]:
        _resp = client.chat.completions.create(
            model=model_name, messages=msgs, **kw,
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
    dpi_override: int | None = None,
    *,
    out: dict | None = None,
) -> TaskInstruction:
    """Parse *prompt* into a ``TaskInstruction`` via a Gemini text call.

    Uses PARSE_PROMPT_MODEL (default: gemini-2.5-flash, low).
    *dpi_override* (CLI ``--dpi``) takes precedence over DPI from the prompt.
    Falls back to a simple keyword heuristic if the Gemini call fails.

    When *out* is supplied (a mutable dict), it is populated with debug fields
    (``model``, ``system``, ``user``, ``raw``, ``thinking``) so the caller can
    persist the prompt+response to disk once an artifact dir is available.
    """
    instruction = _heuristic_fallback(prompt, dpi_override)

    try:
        raw = _call_text(prompt, out=out)
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Prompt parse API error ({exc}) — using heuristic parse.")
        return instruction

    if not raw.strip():
        warn_line("Empty AI response — using heuristic parse.")
        return instruction

    data = parse_json_safe(raw)
    if data is None:
        warn_line("Could not parse AI response — using heuristic parse.")
        return instruction

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

    raw_dpi = data.get("dpi")
    try:
        parsed_dpi = int(raw_dpi) if raw_dpi is not None and raw_dpi != "" else PIPELINE_DEFAULT_DPI
    except (TypeError, ValueError):
        parsed_dpi = PIPELINE_DEFAULT_DPI
    dpi = dpi_override or parsed_dpi
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
    # AI-set value takes priority; fall back to the heuristic in case the AI
    # ignored or omitted the field.
    reuse_cache = bool(data.get("reuse_cache", instruction.reuse_cache))

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
        curved_grade_override = instruction.curved_grade_override

    if "curved_grade_visible" in data:
        raw_vis = data.get("curved_grade_visible")
        if raw_vis is None:
            curved_grade_visible: bool | None = None
        else:
            curved_grade_visible = bool(raw_vis)
    else:
        curved_grade_visible = instruction.curved_grade_visible

    _VALID_TASK_TYPES = {"count_marks", "check_mc", "check_answers"}
    raw_task = data.get("task_type", instruction.task_type)
    if raw_task not in _VALID_TASK_TYPES:
        info_line(f"AI returned unknown task_type {raw_task!r} — keeping {instruction.task_type!r}")
        raw_task = instruction.task_type

    return TaskInstruction(
        task_type=raw_task,
        student_filter=student_filter,
        dpi=dpi,
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


def _heuristic_fallback(prompt: str, dpi_override: int | None) -> TaskInstruction:
    """Simple keyword-based parse used when the AI call fails."""
    p = prompt.lower()

    if "count" in p and "mark" in p:
        task_type = "count_marks"
    elif "multiple choice" in p or " mc " in p or "check mc" in p:
        task_type = "check_mc"
    else:
        task_type = "check_answers"

    student_filter = StudentFilter()
    if "first" in p:
        m = re.search(r"first\s+(\d+)", p)
        if m:
            k = int(m.group(1))
            if k > 0:
                student_filter = StudentFilter(mode="first_n", n=k)

    dpi = dpi_override or PIPELINE_DEFAULT_DPI

    force_clean = ("force" in p and "clean" in p) or "re-clean" in p or "reclean" in p.replace(" ", "")

    folder_path: str | None = None
    pm = _PATH_RE.search(prompt)
    if pm:
        folder_path = next(g for g in pm.groups() if g is not None)

    from_step: int | None = None
    _fs_m = re.search(r'\b(?:resume\s+(?:from\s+)?|from\s+)?step\s+(\d+)', p)
    if _fs_m:
        from_step = int(_fs_m.group(1))

    stop_after: int | None = None
    _sa_m = re.search(r'\b(?:stop|halt|end)\s+(?:(?:after|at|on)\s+)?step\s+(\d+)', p)
    if _sa_m is None:
        _sa_m = re.search(r'\bfirst\s+(\d+)\s+steps?\b', p)
    if _sa_m:
        stop_after = int(_sa_m.group(1))

    # Cache opt-in phrases — kept narrow so casual mentions of "cache" don't
    # accidentally enable it.
    reuse_cache = (
        "reuse cache" in p
        or "use cache" in p
        or "from cache" in p
        or "cache reuse" in p
    )

    # Grade-curve controls. Both stay None unless the prompt explicitly asks
    # for an override — None means "fall back to env var" downstream.
    curved_grade_override: int | None = None
    cm = re.search(r"\bcurve\s*(?:at|to|of|target|=)?\s*(\d{1,3})\b", p)
    if cm is None:
        cm = re.search(r"\btarget\s*(\d{1,3})\b", p)
    if cm is not None:
        v = int(cm.group(1))
        if 0 <= v <= 100:
            curved_grade_override = v

    curved_grade_visible: bool | None = None
    if (
        "hide curve" in p
        or "no curve on student" in p
        or "without curve on student" in p
        or "don't show curve" in p
        or "do not show curve" in p
    ):
        curved_grade_visible = False

    return TaskInstruction(
        task_type=task_type,
        student_filter=student_filter,
        dpi=dpi,
        folder_path=folder_path,
        force_clean_scan=force_clean,
        no_report="no report" in p or "terminal only" in p,
        from_step=from_step,
        stop_after=stop_after,
        reuse_cache=reuse_cache,
        curved_grade_override=curved_grade_override,
        curved_grade_visible=curved_grade_visible,
    )
