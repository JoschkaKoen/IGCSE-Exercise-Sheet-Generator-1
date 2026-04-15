"""Translate a natural-language grading prompt into a structured TaskInstruction.

Uses a text-only Gemini call (PARSE_PROMPT_MODEL) so this step is fast and cheap.
"""

from __future__ import annotations

import json
import os
import re
import time

from .kimi_helpers import parse_json_safe
from xscore.shared.models import StudentFilter, TaskInstruction
from xscore.shared.terminal_ui import api_latency_line, warn_line


def _read_model_config() -> tuple[str, str | None]:
    raw = os.getenv("INTERPRET_PROMPT_MODEL", os.getenv("AI_DEFAULT_MODEL", "gemini-2.5-flash"))
    if "," in raw:
        model, effort = raw.split(",", 1)
        return model.strip(), effort.strip() or None
    return raw.strip(), None


def _call_gemini_text(user_message: str) -> str:
    """Make a text-only Gemini call and return the raw response string."""
    try:
        from google import genai as gai
        from google.genai import types as gai_types
    except ImportError:
        raise RuntimeError("google-genai not installed; run: pip install google-genai")

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set")

    model_name, effort = _read_model_config()
    client = gai.Client(api_key=api_key)

    thinking_map = {"off": 0, "low": 1024, "high": 8192}
    gen_config_kwargs: dict = {"response_mime_type": "application/json"}
    if effort in thinking_map:
        gen_config_kwargs["thinking_config"] = gai_types.ThinkingConfig(
            thinking_budget=thinking_map[effort],
            include_thoughts=False,
        )

    _t0 = time.perf_counter()
    response = client.models.generate_content(
        model=model_name,
        contents=user_message,
        config=gai_types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            **gen_config_kwargs,
        ),
    )
    api_latency_line(time.perf_counter() - _t0)
    return response.text or ""


_SYSTEM_PROMPT = """\
Convert the grading instruction to JSON. Return ONLY the JSON, no explanation.

{
  "task_type": "count_marks|check_mc|check_answers",
  "student_filter": {"mode": "all|specific|first_n", "names": [], "n": 0},
  "dpi": 400,
  "folder_hint": null,
  "folder_path": null,
  "skip_clean_scan": false,
  "force_clean_scan": false,
  "rescaffold": false,
  "through_step": null,
  "no_report": false
}

task_type: count_marks=tally red teacher marks; check_mc=MC only; check_answers=all types.
student_filter.mode: all=default; specific=named students; first_n=first N (set n). names=list.
dpi: 400 default; 300 if "fast"/"quick"; 600 if "high quality"/"accurate".
folder_hint: short name for fuzzy folder match. folder_path: only if user gives explicit path; else null.
Prefer folder_path when both apply.
skip_clean_scan: true=reuse cleaned scan ("skip cleaning", "don't reprocess").
force_clean_scan: true=ignore cache, re-clean ("re-clean", "force deskew"). Never both true.
rescaffold: true=rebuild scaffold ("rebuild scaffold", "reparse", "refresh questions").
through_step: 1-14 or null. 1=parse, 2=folder, 3=roster, 4=exam PDF, 5=mark scheme,
  6=merge scaffold, 7=blank pages, 8=autorotate, 9=deskew, 10=exam geometry,
  11=AI marking blueprints, 12=AI marking, 13=compile reports, 14=timing summary.
no_report: true=skip PDF ("terminal only", "no report").
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
    if not data:
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
        parsed_dpi = int(raw_dpi) if raw_dpi is not None and raw_dpi != "" else 400
    except (TypeError, ValueError):
        parsed_dpi = 400
    dpi = dpi_override or parsed_dpi
    raw_hint = data.get("folder_hint")
    folder_hint = str(raw_hint).strip() if raw_hint not in (None, "") else None
    raw_fp = data.get("folder_path")
    folder_path = str(raw_fp).strip() if raw_fp not in (None, "") else None

    skip_clean_scan = bool(data.get("skip_clean_scan", False))
    force_clean_scan = bool(data.get("force_clean_scan", False))
    rescaffold = bool(data.get("rescaffold", False))
    no_report = bool(data.get("no_report", False))

    ts = data.get("through_step")
    through_step: int | None = None
    if ts is not None and ts != "":
        try:
            n = int(ts)
            if 1 <= n <= 14:
                through_step = n
        except (TypeError, ValueError):
            pass

    if skip_clean_scan and force_clean_scan:
        info_line("AI JSON had both skip_clean_scan and force_clean_scan — cleared both.")
        skip_clean_scan = False
        force_clean_scan = False

    return TaskInstruction(
        task_type=data.get("task_type", instruction.task_type),
        student_filter=student_filter,
        dpi=dpi,
        folder_hint=folder_hint,
        folder_path=folder_path,
        skip_clean_scan=skip_clean_scan,
        force_clean_scan=force_clean_scan,
        rescaffold=rescaffold,
        through_step=through_step,
        no_report=no_report,
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

    dpi = dpi_override or (300 if ("fast" in p or "quick" in p) else 400)

    skip_clean = "skip" in p and ("clean" in p or "deskew" in p or "scan" in p)
    force_clean = ("force" in p and "clean" in p) or "re-clean" in p or "reclean" in p.replace(" ", "")
    # avoid double-trigger: simple heuristics
    if skip_clean and force_clean:
        skip_clean = force_clean = False

    return TaskInstruction(
        task_type=task_type,
        student_filter=student_filter,
        dpi=dpi,
        rescaffold="rescaffold" in p or "reparse" in p or "rebuild scaffold" in p,
        skip_clean_scan=skip_clean,
        force_clean_scan=force_clean,
        no_report="no report" in p or "terminal only" in p,
    )
