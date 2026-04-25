"""Translate a natural-language grading prompt into a structured TaskInstruction.

Uses a text-only Gemini call (PARSE_PROMPT_MODEL) so this step is fast and cheap.
"""

from __future__ import annotations

import os
import re
import time

from .ai_helpers import parse_json_safe
from xscore.config import GEMINI_MAX_OUTPUT_TOKENS, PIPELINE_DEFAULT_DPI
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


def _call_gemini_text(user_message: str) -> str:
    """Make a text-only Gemini call and return the raw response string."""
    try:
        from google.genai import types as gai_types
    except ImportError:
        raise RuntimeError("google-genai not installed; run: pip install google-genai")

    from eXercise.ai_client import build_gemini_thinking_config, make_gemini_native_client
    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")

    model_name, thinking_tokens, max_tokens = _read_model_config()

    gen_config_kwargs: dict = {
        "max_output_tokens": max_tokens or GEMINI_MAX_OUTPUT_TOKENS,
        "response_mime_type": "application/json",
    }
    if thinking_tokens is not None:
        gen_config_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)

    _t0 = time.perf_counter()
    response = client.models.generate_content(
        model=model_name,
        contents=user_message,
        config=gai_types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            **gen_config_kwargs,
        ),
    )
    return response.text or ""


_SYSTEM_PROMPT = """\
Convert the grading instruction to JSON. Return ONLY the JSON, no explanation.

{
  "task_type": "count_marks|check_mc|check_answers",
  "student_filter": {"mode": "all|specific|first_n", "names": [], "n": 0},
  "dpi": 400,
  "folder_hint": null,
  "folder_path": null,
  "force_clean_scan": false,
  "no_report": false,
  "from_step": null,
  "reuse_cache": false
}

task_type: count_marks=tally red teacher marks; check_mc=MC only; check_answers=all types.
student_filter.mode: all=default; specific=named students; first_n=first N (set n). names=list.
dpi: 400 default; 300 if "fast"/"quick"; 600 if "high quality"/"accurate".
folder_hint: short name for fuzzy folder match. folder_path: absolute or ~-relative path; set only when user gives an explicit path; else null. Prefer folder_path when both apply.
Examples: "from ~/Desktop/exams/physics" → folder_path "~/Desktop/exams/physics", folder_hint null; "Space Physics test" → folder_hint "Space Physics", folder_path null.
force_clean_scan: true=ignore cache, re-clean ("re-clean", "force deskew").
no_report: true=skip PDF ("terminal only", "no report").
from_step: integer step number to resume from ("from step 14", "resume from step 13", "rerun from step 15"); null otherwise.
reuse_cache: true=use cached AI marking responses from previous identical runs ("reuse cache", "use cache", "from cache"); false otherwise. Default false.
"""


def parse_prompt(
    prompt: str,
    client: object | None = None,  # ignored — kept for backward compatibility
    dpi_override: int | None = None,
) -> TaskInstruction:
    """Parse *prompt* into a ``TaskInstruction`` via a Gemini text call.

    Uses PARSE_PROMPT_MODEL (default: gemini-2.5-flash, low).
    *dpi_override* (CLI ``--dpi``) takes precedence over DPI from the prompt.
    Falls back to a simple keyword heuristic if the Gemini call fails.
    """
    instruction = _heuristic_fallback(prompt, dpi_override)

    try:
        raw = _call_gemini_text(prompt)
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
    # AI-set value takes priority; fall back to the heuristic in case the AI
    # ignored or omitted the field.
    reuse_cache = bool(data.get("reuse_cache", instruction.reuse_cache))

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
        reuse_cache=reuse_cache,
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

    # Cache opt-in phrases — kept narrow so casual mentions of "cache" don't
    # accidentally enable it.
    reuse_cache = (
        "reuse cache" in p
        or "use cache" in p
        or "from cache" in p
        or "cache reuse" in p
    )

    return TaskInstruction(
        task_type=task_type,
        student_filter=student_filter,
        dpi=dpi,
        folder_path=folder_path,
        force_clean_scan=force_clean,
        no_report="no report" in p or "terminal only" in p,
        from_step=from_step,
        reuse_cache=reuse_cache,
    )
