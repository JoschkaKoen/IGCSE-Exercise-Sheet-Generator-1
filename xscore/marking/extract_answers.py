"""Step 27 — extract_student_answers: transcribe student answers verbatim.

A pre-pass before AI marking (step 28). For each (student, answer_page) the
register yields, send the rendered scan JPEG(s) to a vision model with the
page blueprint as context, and ask only for the verbatim student answer per
question. Output is one XML file per (student, page) under
``27_extract_student_answers/students/``.

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
import xml.etree.ElementTree as ET
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
from xscore.shared.prompt_logger import save_prompt, save_response
from xscore.shared.terminal_ui import format_duration, get_console, icon, info_line, warn_line


_DEFAULT_EXTRACT_MODEL = "qwen3.6-plus, off"


def _safe_load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"corrupt artifact {path}: {e}") from e


def _strip_response_envelope(raw: str) -> str:
    """Strip markdown fences and any text outside the <answers> root."""
    text = raw.strip()
    if text.startswith("```"):
        # ```xml ... ``` or ``` ... ```
        nl = text.find("\n")
        if nl > 0:
            text = text[nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    start = text.find("<answers")
    end = text.rfind("</answers>")
    if start >= 0 and end > start:
        text = text[start : end + len("</answers>")]
    return text


def _parse_extract_response(raw: str) -> dict[str, str]:
    """Parse ``<answers><question number=...><student_answer>...</student_answer>...</answers>``.

    Returns ``{question_number: student_answer_text}``. Question numbers are
    used as keys; duplicate keys keep the last occurrence (mirrors the AI's
    intent if it emits the same question twice — which it shouldn't).
    """
    cleaned = _strip_response_envelope(raw)
    if not cleaned:
        raise FormatParseError("Empty response from extractor")
    try:
        root = ET.fromstring(cleaned)
    except ET.ParseError as e:
        raise FormatParseError(f"Could not parse extractor XML: {e}") from e
    if root.tag != "answers":
        raise FormatParseError(f"Expected <answers> root, got <{root.tag}>")
    result: dict[str, str] = {}
    for q in root.findall("question"):
        number = (q.get("number") or "").strip()
        if not number:
            continue
        sa = q.find("student_answer")
        text = "" if sa is None else (sa.text or "")
        result[number] = text
    return result


def _extract_page_answers(
    client: Any,
    model_id: str,
    b64: str,
    blueprint_str: str,
    thinking_kw: dict,
    use_stream: bool = False,
    extra_b64: tuple[str, ...] = (),
    prompt_save_path: Path | None = None,
    warn=warn_line,
) -> dict[str, str]:
    """Single API call: extract verbatim student answers for one page.

    *blueprint_str* — the per-page marking blueprint already serialised in
    whatever MARKING_FORMAT is active (XML/YAML/JSON). The extraction model
    only consumes it as context (question numbers, max marks, structure), so
    we don't need to round-trip it through any specific format spec.

    *extra_b64* — continuation pages, appended after the primary page in the
    user-content array. Same pattern as the marking call.

    Returns ``{question_number: student_answer}``. Empty dict if the response
    parses but contains no questions (rare; usually a model bug).
    """
    _, user_text = load_prompt(
        "extract_student_answers_xml", section="user", blueprint=blueprint_str,
    )
    _, system_prompt = load_prompt(
        "extract_student_answers_xml", section="system",
    )

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
        return _parse_extract_response(raw)
    except FormatParseError as exc:
        warn(f"Extract: parse error — {exc}")
        raise


def _build_answers_xml(student: str, page: int, answers: dict[str, str]) -> str:
    """Serialise the per-(student, page) answers to the on-disk XML shape."""
    root = ET.Element("answers")
    root.set("student", student)
    root.set("page", str(page))
    for number, text in answers.items():
        q = ET.SubElement(root, "question")
        q.set("number", number)
        sa = ET.SubElement(q, "student_answer")
        sa.text = text
    ET.indent(root)
    return ET.tostring(root, encoding="unicode", short_empty_elements=False) + "\n"


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

    workers = int(os.environ.get("MARKING_WORKERS", str(min(os.cpu_count() or 4, 16))))

    _b64_cache = render_pages_b64(
        ctx.cleaned_pdf, ctx.artifact_dir, dpi, workers,
        instruction=getattr(ctx, "instruction", None),
        cli_filter=getattr(ctx, "student_filter", None),
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
        # context only).
        bp_path = artifact_blueprint_path(ctx.artifact_dir, answer_label, fmt=fmt.artifact_ext())
        blueprint_str = bp_path.read_text(encoding="utf-8")

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
                client, model_id, b64, blueprint_str, thinking_kw,
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

        out_path = artifact_student_answers_path(ctx.artifact_dir, safe_name, p_label)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(_build_answers_xml(student_name, p_label, answers), encoding="utf-8")

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
    """Read the per-(student, page) extraction artifact, returning None if absent.

    Used by :mod:`xscore.marking.ai_mark` to pre-fill the marking blueprint
    for one (student, page) before the marker is called.
    """
    path = artifact_student_answers_path(artifact_dir, student, page)
    if not path.is_file():
        return None
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except ET.ParseError:
        return None
    out: dict[str, str] = {}
    for q in root.findall("question"):
        number = (q.get("number") or "").strip()
        if not number:
            continue
        sa = q.find("student_answer")
        out[number] = "" if sa is None else (sa.text or "")
    return out


def patch_blueprint_with_answers(
    blueprint_str: str, answers_map: dict[str, str], fmt: Any
) -> str:
    """Return *blueprint_str* with each question's ``student_answer`` field
    filled in from *answers_map* (keyed by question ``number``).

    Format-aware: dispatches on ``fmt.artifact_ext()`` (xml | yaml | json).
    Preserves the original on-disk structure exactly — we walk the parsed
    representation, mutate just the ``student_answer`` field per question,
    and re-emit. Question numbers not present in *answers_map* are left
    unchanged.
    """
    ext = fmt.artifact_ext()
    if ext == "xml":
        return _patch_blueprint_xml(blueprint_str, answers_map)
    if ext == "yaml":
        return _patch_blueprint_yaml(blueprint_str, answers_map)
    if ext == "json":
        return _patch_blueprint_json(blueprint_str, answers_map)
    raise ValueError(f"Unsupported blueprint format: {ext}")


def _patch_blueprint_xml(blueprint_str: str, answers_map: dict[str, str]) -> str:
    root = ET.fromstring(blueprint_str)
    for q in root.findall("question"):
        qnum = (q.get("number") or "").strip()
        if qnum not in answers_map:
            continue
        sa = q.find("student_answer")
        if sa is None:
            sa = ET.SubElement(q, "student_answer")
        sa.text = answers_map[qnum]
    ET.indent(root)
    return ET.tostring(
        root, encoding="unicode", xml_declaration=False, short_empty_elements=False,
    )


def _patch_blueprint_yaml(blueprint_str: str, answers_map: dict[str, str]) -> str:
    import yaml
    data = yaml.safe_load(blueprint_str) or {}
    for q in data.get("questions", []) or []:
        qnum = str(q.get("number", "")).strip()
        if qnum in answers_map:
            q["student_answer"] = answers_map[qnum]
    return yaml.safe_dump(
        data, allow_unicode=True, default_flow_style=False, sort_keys=False,
    )


def _patch_blueprint_json(blueprint_str: str, answers_map: dict[str, str]) -> str:
    data = json.loads(blueprint_str)
    for q in data.get("questions", []) or []:
        qnum = str(q.get("number", "")).strip()
        if qnum in answers_map:
            q["student_answer"] = answers_map[qnum]
    return json.dumps(data, ensure_ascii=False, indent=2)
