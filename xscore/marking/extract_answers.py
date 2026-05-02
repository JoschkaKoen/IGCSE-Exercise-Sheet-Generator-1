"""Step 28 — extract_student_answers: transcribe student answers verbatim.

A pre-pass before AI marking (step 29). For each (student, answer_page) the
register yields, send the rendered scan JPEG(s) to a vision model with the
page blueprint as context, and ask only for the verbatim student answer per
question. Output is one YAML file per (student, page) under
``28_extract_student_answers/students/``.

The marking step then loads these artifacts, pre-fills ``student_answer`` on
each blueprint question, and asks the marker only to assign marks + write
explanations — sparing the marking model the transcription work.

Uses the EXTRACT_ANSWERS_MODEL env var (default: qwen3.6-plus, off) via
make_ai_client(). Falls back to MARKING_MODEL if unset.

Students × pages are processed in parallel (MARKING_WORKERS workers, default
varies with cpu_count). Each worker opens its own fitz document handle (fitz
is not thread-safe).
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from eXercise.ai_client import collect_streamed_response, make_ai_client, build_completion_kwargs
from eXercise.api_retry import retry_api_call
from xscore.marking.extract_answers_display import (
    build_display_entries,
    emit_skipped_lines,
    make_reorder_buffer,
)
from xscore.marking.formats.base import FormatParseError
from xscore.marking.formats import get_marking_format
from xscore.prompts.loader import load_prompt
from xscore.shared.exam_paths import (
    artifact_blueprint_path,
    artifact_student_answers_failed_path,
    artifact_student_answers_path,
    artifact_student_answers_prompt_path,
)
from xscore.shared.prompt_logger import (
    save_input_data, save_output_data, save_prompt, save_response,
)
from xscore.shared.response_parsing import strip_code_fences
from xscore.shared.terminal_ui import format_duration, get_console, icon, info_line, warn_line


_DEFAULT_EXTRACT_MODEL = "qwen3.6-plus, off"


def _safe_load_json(path: Path) -> Any:
    """Read a JSON artifact, surfacing the path on parse failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"corrupt artifact {path}: {e}") from e


# ---------------------------------------------------------------------------
# Custom YAML dumper — block scalar (|) for strings with backslashes / newlines,
# plain scalars otherwise. Mirrors xscore.marking.formats.yaml_format._MarkingDumper
# so on-disk artifacts in this step look stylistically identical to marking
# artifacts (LaTeX content lands in literal blocks, single-letter MCQ answers
# in plain style). Kept local to this module to avoid promoting a private
# helper to a shared utility before there's a third caller.
# ---------------------------------------------------------------------------

import yaml as _yaml


class _ExtractAnswersDumper(_yaml.SafeDumper):
    pass


def _ea_str_representer(dumper: _yaml.Dumper, data: str) -> _yaml.ScalarNode:
    if "\n" in data or "\\" in data:
        # Strip per-line trailing whitespace so PyYAML can use block-scalar
        # style. Without this, multiline strings with trailing whitespace fall
        # back to double-quoted form, which interprets backslashes as escapes
        # and silently destroys LaTeX commands.
        data = "\n".join(line.rstrip() for line in data.split("\n"))
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_ExtractAnswersDumper.add_representer(str, _ea_str_representer)


def _parse_extract_response(raw: str, fmt: Any) -> dict[str, str]:
    """Parse the AI's transcription response.

    Returns ``{question_number: student_answer_text}``. Question numbers are
    coerced to strings (YAML may decode ``1`` as int). Duplicate numbers
    keep the last occurrence.
    """
    cleaned = strip_code_fences(raw).strip()
    if not cleaned:
        raise FormatParseError("Empty response from extractor")
    return _parse_yaml_response(cleaned)


def _parse_yaml_response(cleaned: str) -> dict[str, str]:
    try:
        data = _yaml.safe_load(cleaned)
    except _yaml.YAMLError as e:
        raise FormatParseError(f"YAML: {e}") from e
    if not isinstance(data, dict):
        raise FormatParseError(f"YAML: expected a mapping, got {type(data).__name__}")
    questions = data.get("questions", [])
    if not isinstance(questions, list):
        raise FormatParseError("YAML: 'questions' key is not a list")
    result: dict[str, str] = {}
    for q in questions:
        if not isinstance(q, dict):
            continue
        number = str(q.get("number", "")).strip()
        if not number:
            continue
        text = str(q.get("student_answer") or "").strip()
        result[number] = text
    return result


def _extract_page_answers(
    client: Any,
    model_id: str,
    b64: str,
    blueprint_str: str,
    thinking_kw: dict,
    fmt: Any,
    use_stream: bool = False,
    extra_b64: tuple[str, ...] = (),
    prompt_save_path: Path | None = None,
    warn=warn_line,
) -> dict[str, str]:
    """Single API call: extract verbatim student answers for one page.

    *blueprint_str* — the per-page marking blueprint already serialised as YAML.

    *extra_b64* — continuation pages, appended after the primary page in the
    user-content array. Same pattern as the marking call.

    Returns ``{question_number: student_answer}``. Empty dict if the response
    parses but contains no questions (rare; usually a model bug).
    """
    prompt_name = "extract_student_answers"
    _, user_text = load_prompt(prompt_name, section="user", blueprint=blueprint_str)
    _, system_prompt = load_prompt(prompt_name, section="system")

    user_content: list[dict] = [
        {"type": "text", "text": user_text},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]
    for cb64 in extra_b64:
        user_content.append(
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{cb64}"}}
        )

    kwargs: dict[str, Any] = dict(
        model=model_id,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
    )
    kwargs.update(thinking_kw)

    save_prompt(prompt_save_path, model=model_id, messages=kwargs["messages"])
    save_input_data(prompt_save_path, blueprint_str, ext="yaml")

    def _do_call() -> tuple[str, str]:
        if use_stream:
            _th: list[str] = []
            stream = client.chat.completions.create(**kwargs, stream=True)
            return collect_streamed_response(stream, thinking_out=_th), "".join(_th)
        resp = client.chat.completions.create(**kwargs)
        return (
            resp.choices[0].message.content or "",
            getattr(resp.choices[0].message, "reasoning_content", "") or "",
        )

    raw, thinking_text = retry_api_call(_do_call, label=f"Extract ({model_id})")
    save_response(prompt_save_path, raw, thinking=thinking_text)
    try:
        parsed = _parse_extract_response(raw, fmt)
    except FormatParseError as exc:
        warn(f"Extract: parse error — {exc}")
        raise
    save_output_data(prompt_save_path, raw, ext="yaml")
    return parsed


def _build_answers(student: str, page: int, answers: dict[str, str], fmt: Any) -> str:
    """Serialise the per-(student, page) answers to the on-disk YAML shape.

    Uses a literal block scalar for ``student_answer`` strings containing
    backslashes or newlines, mirroring the marking YAML format's dumper.
    """
    doc: dict = {
        "page": int(page),
        "student_name": str(student),
        "questions": [
            {"number": str(number), "student_answer": str(text)}
            for number, text in answers.items()
        ],
    }
    return _yaml.dump(
        doc, Dumper=_ExtractAnswersDumper,
        allow_unicode=True, default_flow_style=False,
        sort_keys=False,
    )


def run_extract_student_answers(ctx: Any, *, dpi: int | None = None) -> list[dict]:
    """Run the per-(student, page) extraction loop. Mirrors run_ai_marking.

    Returns a list of timing records (one per successful call) with keys
    ``phase``, ``student``, ``page``, ``duration_s``.

    Per-page failures are collected onto ``ctx.extract_answers_failures`` and
    do NOT abort the run — marking will fall back to AI transcription for the
    affected pages.
    """
    from xscore.config import MARKING_DPI
    if dpi is None:
        dpi = MARKING_DPI

    import fitz

    from xscore.marking.ai_mark import render_pages_b64
    from xscore.marking.marking_page_register import (
        build_initial_register,
        iter_marking_calls,
        load_register,
    )
    from xscore.shared.exam_paths import artifact_exam_student_list_json_path

    fmt = get_marking_format()

    result = make_ai_client(
        model_env="EXTRACT_ANSWERS_MODEL",
        legacy_model_env="MARKING_MODEL",
        default_model=_DEFAULT_EXTRACT_MODEL,
    )
    if result is None:
        raise RuntimeError(
            "EXTRACT_ANSWERS_MODEL client could not be created — "
            "check DASHSCOPE_API_KEY / GEMINI_API_KEY in .env"
        )
    client, model_id, _provider, _thinking, max_tok = result
    use_stream, thinking_kw = build_completion_kwargs(_provider, _thinking, max_tok)

    list_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
    if not list_path.exists():
        raise FileNotFoundError(
            f"student_names artifact not found at {list_path} — run step 15 first"
        )
    raw_assignments: list[dict] = _safe_load_json(list_path)

    instr = getattr(ctx, "instruction", None)
    if instr is not None:
        sf = instr.student_filter
        if sf.mode == "specific" and sf.names:
            raw_assignments = [a for a in raw_assignments if a["student_name"] in sf.names]
        elif sf.mode == "first_n" and sf.n:
            raw_assignments = raw_assignments[: sf.n]

    cli_filter = getattr(ctx, "student_filter", None)
    if cli_filter:
        wanted = {n.strip().lower() for n in cli_filter}
        raw_assignments = [
            a for a in raw_assignments
            if (a.get("student_name") or "").strip().lower() in wanted
        ]

    limit_students = getattr(ctx, "limit_students", None)
    if limit_students:
        raw_assignments = raw_assignments[:limit_students]

    workers = int(os.environ.get("MARKING_WORKERS", str(min(os.cpu_count() or 4, 16))))

    _b64_cache = render_pages_b64(
        ctx.cleaned_pdf, ctx.artifact_dir, dpi, workers,
        instruction=getattr(ctx, "instruction", None),
        cli_filter=getattr(ctx, "student_filter", None),
        limit_students=getattr(ctx, "limit_students", None),
    )

    register = load_register(ctx.artifact_dir)
    if register is None:
        register = build_initial_register(ctx)

    _scaffold_pc = ctx.scaffold.page_count if ctx.scaffold is not None else None
    page_tasks: list[tuple[dict, int, int, int, list[int], list[str]]] = list(
        iter_marking_calls(
            register,
            raw_assignments=raw_assignments,
            scaffold_page_count=_scaffold_pc,
        )
    )

    timings_lock = threading.Lock()
    api_call_timings: list[dict] = []
    all_failures: list[dict] = []

    # Enumerate every page in the scan PDF (extracted, cover-skipped,
    # no-handwriting-skipped) so the output is a complete picture. The
    # reorder buffer prints lines in (student, p_label) order regardless of
    # worker completion order.
    display_entries, idx_by_key, total_pdf_pages, n_cover, n_no_hw = (
        build_display_entries(register, raw_assignments)
    )

    info_line(
        f"Extracting answers for {len(page_tasks)} of {len(display_entries)} pages "
        f"({n_cover} cover, {n_no_hw} no-handwriting skipped) …"
    )

    _emit_ordered = make_reorder_buffer(get_console())
    emit_skipped_lines(display_entries, idx_by_key, total_pdf_pages, _emit_ordered, icon)

    def _extract_one(
        idx: int,
        assignment: dict, p_label: int, answer_label: int, answer_page_count: int,
        extra_scan_pages: list[int], extra_sources: list[str],
    ) -> tuple[dict | None, dict | None]:
        student_name: str = assignment["student_name"]
        safe_name = student_name or f"Unknown_{p_label}"

        # Load blueprint for this answer page (any format — we pass it as
        # context only). Strip correct_answer so the transcriber isn't biased
        # by the answer key.
        bp_path = artifact_blueprint_path(ctx.artifact_dir, answer_label, fmt=fmt.artifact_ext())
        blueprint_str = bp_path.read_text(encoding="utf-8")
        blueprint_str = blueprint_for_transcription(blueprint_str, fmt)

        # Assemble the page bundle: primary scan page + continuation pages,
        # in ascending scan-page order so the model reads top-to-bottom.
        exercise_scan_page = assignment["page_numbers"][p_label - 1]
        scan_to_plabel = {sp: i + 1 for i, sp in enumerate(assignment["page_numbers"])}
        all_pages = sorted([exercise_scan_page] + extra_scan_pages)
        all_b64 = [
            _b64_cache[(student_name, scan_to_plabel[sp])]
            for sp in all_pages
            if sp in scan_to_plabel and (student_name, scan_to_plabel[sp]) in _b64_cache
        ]
        scan_page_global = assignment["page_numbers"][p_label - 1]
        student_total = len(assignment["page_numbers"])
        prefix = (
            f"Student '{student_name}'"
            f"  ·  page {scan_page_global:>3}/{total_pdf_pages}"
            f"  ·  ans p {p_label:>2}/{student_total}"
        )

        if not all_b64:
            failure = {
                "student": student_name, "page": p_label,
                "error": "no scan pages rendered (cache miss)",
            }
            _emit_ordered(idx, f"[yellow]  {icon('warn')}  {prefix}  ·  FAILED (cache miss)[/]")
            return None, failure
        b64 = all_b64[0]
        extra_b64 = tuple(all_b64[1:])

        prompt_save = artifact_student_answers_prompt_path(ctx.artifact_dir, student_name, p_label)
        t0 = time.perf_counter()
        try:
            answers = _extract_page_answers(
                client, model_id, b64, blueprint_str, thinking_kw, fmt,
                use_stream=use_stream,
                extra_b64=extra_b64,
                prompt_save_path=prompt_save,
            )
        except FormatParseError as exc:
            failure = {
                "student": student_name, "page": p_label,
                "error": f"parse error: {exc}",
            }
            failed_path = artifact_student_answers_failed_path(ctx.artifact_dir, safe_name, p_label)
            failed_path.parent.mkdir(parents=True, exist_ok=True)
            failed_path.write_text(json.dumps(failure, indent=2, ensure_ascii=False), encoding="utf-8")
            _emit_ordered(idx, f"[yellow]  {icon('warn')}  {prefix}  ·  FAILED[/]")
            return None, failure
        except Exception as exc:  # noqa: BLE001
            failure = {
                "student": student_name, "page": p_label,
                "error": f"unhandled: {exc}",
            }
            failed_path = artifact_student_answers_failed_path(ctx.artifact_dir, safe_name, p_label)
            failed_path.parent.mkdir(parents=True, exist_ok=True)
            failed_path.write_text(json.dumps(failure, indent=2, ensure_ascii=False), encoding="utf-8")
            _emit_ordered(idx, f"[yellow]  {icon('warn')}  {prefix}  ·  FAILED[/]")
            return None, failure

        dur = round(time.perf_counter() - t0, 2)

        out_path = artifact_student_answers_path(
            ctx.artifact_dir, safe_name, p_label, fmt=fmt.artifact_ext(),
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            _build_answers(student_name, p_label, answers, fmt), encoding="utf-8",
        )

        _emit_ordered(idx, (
            f"[green]  {icon('ok')}  {prefix}"
            f"  ·  {format_duration(dur)}  ·  {len(answers)} answer(s)[/]"
        ))
        return {"phase": "extract_answers", "student": student_name, "page": p_label,
                "duration_s": dur}, None

    # Each worker carries the absolute display ``idx`` so its line lands in
    # the right slot in the reorder buffer regardless of completion order.
    extracted_with_idx = [
        (idx_by_key[(a["student_name"], p_label)], a, p_label, ans_lbl, ans_cnt, extras, sources)
        for (a, p_label, ans_lbl, ans_cnt, extras, sources) in page_tasks
    ]
    extracted_with_idx.sort(key=lambda t: t[0])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_extract_one, idx, a, p_label, ans_lbl, ans_cnt, extras, sources):
                (idx, a["student_name"], p_label)
            for (idx, a, p_label, ans_lbl, ans_cnt, extras, sources) in extracted_with_idx
        }
        for fut in as_completed(futures):
            try:
                timing, failure = fut.result()
            except Exception as exc:  # noqa: BLE001
                idx, student, page = futures[fut]
                failure = {"student": student, "page": page, "error": f"unhandled worker: {exc}"}
                timing = None
                _emit_ordered(idx, (
                    f"[yellow]  {icon('warn')}  Student '{student}'"
                    f"  ·  ans p {page}  ·  FAILED (worker crash)[/]"
                ))
            with timings_lock:
                if timing:
                    api_call_timings.append(timing)
                if failure:
                    all_failures.append(failure)

    ctx.extract_answers_failures = all_failures
    return api_call_timings


def load_student_answers(
    artifact_dir: Path, student: str, page: int
) -> dict[str, str] | None:
    """Read the per-(student, page) YAML extraction artifact.

    Returns ``{question_number: student_answer}`` or ``None`` if no artifact
    exists for this (student, page).

    Used by :mod:`xscore.marking.ai_mark` to pre-fill the marking blueprint
    for one (student, page) before the marker is called.
    """
    path = artifact_student_answers_path(artifact_dir, student, page, fmt="yaml")
    if not path.is_file():
        return None
    try:
        return _parse_yaml_response(path.read_text(encoding="utf-8"))
    except FormatParseError:
        return None


def patch_blueprint_with_answers(
    blueprint_str: str, answers_map: dict[str, str], fmt: Any
) -> str:
    """Return *blueprint_str* with each question's ``student_answer`` field
    filled in from *answers_map* (keyed by question ``number``).

    Preserves the original YAML structure exactly — we walk the parsed
    representation, mutate just the ``student_answer`` field per question,
    and re-emit. Question numbers not present in *answers_map* are left
    unchanged.
    """
    data = _yaml.safe_load(blueprint_str) or {}
    for q in data.get("questions", []) or []:
        qnum = str(q.get("number", "")).strip()
        if qnum in answers_map:
            q["student_answer"] = answers_map[qnum]
    return _yaml.dump(
        data, Dumper=_ExtractAnswersDumper,
        allow_unicode=True, default_flow_style=False, sort_keys=False,
    )


def blueprint_for_transcription(blueprint_str: str, fmt: Any) -> str:
    """Return *blueprint_str* with fields the transcriber must not see removed.

    Strips ``correct_answer`` so the transcriber AI is not biased by the answer
    key while transcribing. Also strips the step-29 marking fields
    (``assigned_marks``, ``explanation``, ``confidence``, ``problem``) — the
    transcriber owns only ``student_answer`` and shouldn't see (or have a
    chance to fill) the marking-only target fields.
    """
    data = _yaml.safe_load(blueprint_str) or {}
    for q in data.get("questions", []) or []:
        for _key in ("correct_answer", "assigned_marks", "explanation", "confidence", "problem"):
            q.pop(_key, None)
    return _yaml.dump(
        data, Dumper=_ExtractAnswersDumper,
        allow_unicode=True, default_flow_style=False, sort_keys=False,
    )
