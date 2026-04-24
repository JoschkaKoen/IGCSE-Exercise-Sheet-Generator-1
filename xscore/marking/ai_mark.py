"""Step 18/19 — AI marking: iterate over student scan pages and fill blueprint JSONs.

Uses the MARKING_MODEL env var (default: qwen3.6-plus, off) via make_ai_client().
Requires DASHSCOPE_API_KEY to be set in .env.

Students are processed in parallel (MARKING_WORKERS workers, default 4).
Each worker opens its own fitz document handle (fitz is not thread-safe).
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections.abc import Callable
from typing import Any

from xscore.config import GEMINI_MAX_OUTPUT_TOKENS, MARKING_MODEL_DEFAULT
from xscore.marking.blueprints import marked_to_md
from xscore.marking.formats import get_marking_format
from xscore.marking.formats.base import FormatParseError
from xscore.shared.exam_paths import (
    artifact_blank_pages_json_path,
    artifact_blueprint_path,
    artifact_mark_scheme_graphics_dir,
    artifact_marked_failed_path,
    artifact_marked_md_path,
    artifact_marked_path,
    artifact_marking_prompt_path,
)
from xscore.shared.prompt_logger import save_prompt
from xscore.shared.terminal_ui import format_duration, get_console, icon, info_line, ok_line, warn_line

from xscore.marking.mark_xml import MarkingFailure
from xscore.marking.mark_page import (
    _bq_key, _build_marking_system_prompt, _fix_mc_marks, _mark_page, _render_page_b64,
)

_DEFAULT_MARKING_MODEL = MARKING_MODEL_DEFAULT





def render_pages_b64(
    cleaned_pdf: Path,
    artifact_dir: Path,
    dpi: int,
    workers: int,
    *,
    instruction: Any = None,
) -> dict[tuple[str, int], str]:
    """Render all scan pages to base64 JPEG, parallelised.

    Reads 10_exam_student_list.json directly (same source as run_ai_marking).
    Each worker opens its own fitz.Document — fitz is not thread-safe.
    Returns {(student_name, page_label): b64_str}.
    """
    import fitz
    from concurrent.futures import as_completed
    from xscore.shared.exam_paths import artifact_exam_student_list_json_path

    list_path = artifact_exam_student_list_json_path(artifact_dir)
    raw: list[dict] = json.loads(list_path.read_text(encoding="utf-8"))

    if instruction is not None:
        sf = instruction.student_filter
        if sf.mode == "specific" and sf.names:
            raw = [a for a in raw if a["student_name"] in sf.names]
        elif sf.mode == "first_n" and sf.n:
            raw = raw[: sf.n]

    tasks: list[tuple[str, int, int]] = []
    for a in raw:
        for p_label, scan_page in enumerate(a["page_numbers"], 1):
            tasks.append((a["student_name"], p_label, scan_page - 1))

    cache: dict[tuple[str, int], str] = {}
    if not tasks:
        return cache

    def _render_one(student: str, p_label: int, page_0idx: int) -> tuple[tuple[str, int], str]:
        doc = fitz.open(str(cleaned_pdf))
        try:
            b64 = _render_page_b64(doc, page_0idx, dpi=dpi)
        finally:
            doc.close()
        return (student, p_label), b64

    n_workers = min(len(tasks), workers)
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = {pool.submit(_render_one, s, pl, p0): None for s, pl, p0 in tasks}
        for fut in as_completed(futs):
            key, b64 = fut.result()
            cache[key] = b64

    return cache


def _mark_page_pdf(
    pdf_path: str,
    blueprint: dict,
    blueprint_str: str,
    prompt_save_path: Path | None,
    warn: Callable[[str], None],
    scheme_graphics: "list[tuple[str, int, str]]" = (),
    has_continuation: bool = False,
    fmt=None,
) -> dict:
    """Upload a PDF page (+ optional continuation pages) to Gemini and mark it.

    Raises MarkingFailure if all retries are exhausted.
    """
    import os
    from google.genai import types as gai_types
    from xscore.shared.prompt_logger import save_response
    from eXercise.ai_client import make_gemini_native_client, parse_model_effort, is_503_error

    if fmt is None:
        from xscore.marking.formats.xml_format import XmlMarkingFormat
        fmt = XmlMarkingFormat()

    gai_client = make_gemini_native_client()
    if gai_client is None:
        raise RuntimeError("GEMINI_API_KEY not set — required for Gemini MARKING_MODEL")

    _model_env = os.environ.get("MARKING_MODEL", "")
    model_id, _effort = parse_model_effort(_model_env) if _model_env else ("gemini-2.5-flash", None)

    system_prompt = _build_marking_system_prompt(
        blueprint, scheme_graphics, has_continuation=has_continuation, fmt=fmt
    )
    user_text = fmt.build_user_text(blueprint_str)
    save_prompt(prompt_save_path, model=model_id, system=system_prompt,
                messages=[{"role": "user", "content": user_text}])

    _THINKING_MAP = {"off": 0, "low": 1024, "high": 8192}
    cfg: dict = {
        "system_instruction": system_prompt,
        "max_output_tokens": GEMINI_MAX_OUTPUT_TOKENS,
    }
    cfg.update(fmt.api_extra_kwargs(model_id))
    if _effort in _THINKING_MAP:
        cfg["thinking_config"] = gai_types.ThinkingConfig(
            thinking_budget=_THINKING_MAP[_effort],
            include_thoughts=False,
        )
    config = gai_types.GenerateContentConfig(**cfg)

    _last_exc: BaseException = RuntimeError("no attempts made")
    _last_raw: str = ""
    _actual_attempts = 0
    uploaded = None
    for attempt in range(2):  # initial attempt + 1 retry on 503
        _actual_attempts += 1
        try:
            if uploaded is None:
                uploaded = gai_client.files.upload(
                    file=pdf_path,
                    config=gai_types.UploadFileConfig(mime_type="application/pdf"),
                )
            contents = [
                gai_types.Part.from_uri(file_uri=uploaded.uri, mime_type="application/pdf"),
                gai_types.Part.from_text(text=user_text),
            ]
            for _, _, g_b64 in scheme_graphics:
                contents.append(
                    gai_types.Part.from_bytes(
                        data=base64.b64decode(g_b64), mime_type="image/png"
                    )
                )
            resp = gai_client.models.generate_content(
                model=model_id, contents=contents, config=config,
            )
            raw = resp.text or ""
            _last_raw = raw
            save_response(prompt_save_path, raw)
            parsed_questions = fmt.parse_response(raw)
            result = blueprint.copy()
            fill_groups: dict[tuple, list] = defaultdict(list)
            for q in parsed_questions:
                fill_groups[_bq_key(q)].append(q)
            fill_group_idx: dict[tuple, int] = defaultdict(int)
            for bq in result.get("questions", []):
                grp_key = _bq_key(bq)
                idx = fill_group_idx[grp_key]
                fill_group_idx[grp_key] += 1
                if fill_groups[grp_key] and idx < len(fill_groups[grp_key]):
                    src_q = fill_groups[grp_key][idx]
                    bq["student_answer"] = src_q.get("student_answer", "")
                    bq["assigned_marks"] = src_q.get("assigned_marks", 0)
                    bq["explanation"] = src_q.get("explanation", "")
            _fix_mc_marks(result)
            for bq in result.get("questions", []):
                if not (bq.get("student_answer") or "").strip() and bq.get("assigned_marks") in (None, 0):
                    bq["explanation"] = "Blank answer."
            for bq in result.get("questions", []):
                max_m = bq.get("max_marks")
                if max_m is None:
                    continue
                m = bq.get("assigned_marks", 0)
                if not isinstance(m, int) or m < 0 or m > int(max_m):
                    warn(
                        f"Marking: Q{bq.get('number')} assigned_marks={m} out of range "
                        f"[0, {max_m}] — clamping"
                    )
                    try:
                        m_int = int(m)
                    except (TypeError, ValueError):
                        m_int = 0
                    bq["assigned_marks"] = max(0, min(m_int, int(max_m)))
            try:
                gai_client.files.delete(name=uploaded.name)
            except Exception as _del_exc:  # noqa: BLE001
                warn(f"Gemini file cleanup failed (file may remain in storage): {_del_exc}")
            return result
        except FormatParseError as exc:
            warn(f"Marking parse error (PDF upload path) — marking aborted ({exc})")
            _last_exc = exc
            break
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            warn(f"Gemini error (attempt {_actual_attempts}): {exc}")
            _last_exc = exc
            if attempt == 0 and is_503_error(exc):
                time.sleep(0.1)
            else:
                break
    if uploaded is not None:
        try:
            gai_client.files.delete(name=uploaded.name)
        except Exception as _del_exc:  # noqa: BLE001
            warn(f"Gemini file cleanup failed after all retries (file may remain in storage): {_del_exc}")
    raise MarkingFailure(
        attempts=_actual_attempts, last_exc=_last_exc, last_raw=_last_raw
    )


def _scheme_graphics_for_page(
    blueprint: dict,
    graphics_map: dict[str, list[Path]],
) -> list[tuple[str, int, str]]:
    """Return (question_number, ms_page, base64_png) tuples for mark-scheme graphics on this page."""
    out = []
    for q in blueprint.get("questions", []):
        qnum = str(q.get("number", ""))
        safe_num = re.sub(r"[^\w]", "_", qnum)
        for png_path in graphics_map.get(safe_num, []):
            page_prefix = png_path.name.split("_")[0]
            ms_page = int(page_prefix) if page_prefix.isdigit() else 0
            out.append((qnum, ms_page, base64.b64encode(png_path.read_bytes()).decode()))
    return out


def run_ai_marking(ctx: Any, *, dpi: int | None = None) -> list[dict]:
    """Run the full AI marking loop for all students and pages.

    Reads page assignments from ``10_exam_student_list.json`` (written by step 10)
    so each student's scan pages are determined by name detection, not position.
    Pages are processed in parallel (MARKING_WORKERS env var, default varies with cpu_count).
    *dpi* defaults to ``MARKING_DPI`` when not supplied.
    Returns a list of API call timing records for step 15.
    """
    from xscore.config import MARKING_DPI
    if dpi is None:
        dpi = MARKING_DPI

    import fitz

    from eXercise.ai_client import make_ai_client, build_thinking_kwargs
    from xscore.shared.exam_paths import artifact_exam_student_list_json_path

    fmt = get_marking_format()

    result = make_ai_client(model_env="MARKING_MODEL", default_model=_DEFAULT_MARKING_MODEL)
    if result is None:
        raise RuntimeError(
            "MARKING_MODEL client could not be created — "
            "check DASHSCOPE_API_KEY / GEMINI_API_KEY in .env"
        )
    client, model_id, _provider, _effort = result
    _use_stream, _thinking_kw = build_thinking_kwargs(_provider, _effort)

    # Load page assignments produced by step 10 name detection.
    list_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
    if not list_path.exists():
        raise FileNotFoundError(
            f"10_exam_student_list.json not found at {list_path} — run step 10 first"
        )
    raw_assignments: list[dict] = json.loads(list_path.read_text(encoding="utf-8"))
    # Each entry: {"student_name": str, "page_numbers": [int, ...], "confidence": str}

    # Load blank page detection results (written by step 13 blank_page_detection).
    _blank_json = artifact_blank_pages_json_path(ctx.artifact_dir)
    # Keys: student_name → set of scan_pages to skip (blank, no handwriting)
    _skip_scan_pages_by_student: dict[str, set[int]] = {}
    # Keys: student_name → {exercise_scan_page → [extra_blank_scan_pages_with_handwriting]}
    _extra_by_student: dict[str, dict[int, list[int]]] = {}
    if _blank_json.exists():
        _bdata = json.loads(_blank_json.read_text(encoding="utf-8"))
        for _s in _bdata.get("students", []):
            _skip: set[int] = set()
            _extras: dict[int, list[int]] = {}
            for _bp in _s["blank_scan_pages"]:
                if not _bp["has_handwriting"]:
                    _skip.add(_bp["scan_page"])
                elif _bp.get("attach_to_scan_page") is not None:
                    _extras.setdefault(_bp["attach_to_scan_page"], []).append(_bp["scan_page"])
            _skip_scan_pages_by_student[_s["student_name"]] = _skip
            _extra_by_student[_s["student_name"]] = _extras

    _instr = getattr(ctx, "instruction", None)
    if _instr is not None:
        sf = _instr.student_filter
        if sf.mode == "specific" and sf.names:
            raw_assignments = [a for a in raw_assignments if a["student_name"] in sf.names]
        elif sf.mode == "first_n" and sf.n:
            raw_assignments = raw_assignments[: sf.n]

    workers = int(os.environ.get("MARKING_WORKERS", str(min(os.cpu_count() or 4, 16))))
    timings_lock = threading.Lock()
    api_call_timings: list[dict] = []

    b64_future = getattr(ctx, "b64_future", None)
    if b64_future is not None:
        _b64_cache = b64_future.result()   # instant if BG finished; brief wait if not
        ok_line(f"Pre-rendering done  ·  {len(_b64_cache)} page(s) ready")
    else:
        _total_pages = sum(len(a["page_numbers"]) for a in raw_assignments)
        info_line(f"Rendering {_total_pages} page(s) for {len(raw_assignments)} students at {dpi} DPI …")
        _b64_cache = render_pages_b64(
            ctx.cleaned_pdf, ctx.artifact_dir, dpi, workers,
            instruction=getattr(ctx, "instruction", None),
        )

    # Pre-build mark-scheme graphics map: safe_qnum → sorted list of PNG paths
    _graphics_dir = artifact_mark_scheme_graphics_dir(ctx.artifact_dir, ctx.step_offset)
    _graphics_map: dict[str, list[Path]] = {}
    if _graphics_dir.is_dir():
        _gfx_re = re.compile(r"^\d+_(.+)_(\d+)\.png$")
        for _p in sorted(_graphics_dir.glob("*.png")):
            _m = _gfx_re.match(_p.name)
            if _m:
                _graphics_map.setdefault(_m.group(1), []).append(_p)
        for _v in _graphics_map.values():
            _v.sort()

    # Validate cover-page state before building the task list.
    # empty_exam_has_cover drives the per-student page offset; if it is None
    # (step 8 did not complete), the offset would silently default to the wrong value.
    if ctx.empty_exam_has_cover is None and any(
        a.get("cover_page_number") is not None for a in raw_assignments
    ):
        raise RuntimeError(
            "empty_exam_has_cover was not determined (step 8 incomplete?) — "
            "cannot safely compute page offsets for students with cover pages"
        )

    # Build flat per-page task list — cover pages, out-of-range pages, and blank exam pages
    # without handwriting are excluded here.
    page_tasks: list[tuple[dict, int, int, int, list[int]]] = []
    for a in raw_assignments:
        has_cover = a.get("cover_page_number") is not None
        answer_page_count = len(a["page_numbers"]) - (1 if has_cover else 0)
        student_skip = _skip_scan_pages_by_student.get(a["student_name"], set())
        student_extras = _extra_by_student.get(a["student_name"], {})
        for p_label, _ in enumerate(a["page_numbers"], 1):
            if has_cover and p_label == 1:
                continue
            _cover_offset = 1 if (has_cover and not ctx.empty_exam_has_cover) else 0
            answer_label = p_label - _cover_offset
            scan_page = a["page_numbers"][p_label - 1]
            if ctx.scaffold is not None and answer_label > ctx.scaffold.page_count:
                continue
            if scan_page in student_skip:
                continue
            extra_scan_pages = student_extras.get(scan_page, [])
            page_tasks.append((a, p_label, answer_label, answer_page_count, extra_scan_pages))

    import contextlib
    import sys
    from rich.live import Live

    _use_live = sys.stdout.isatty() and not hasattr(sys.stdout, '_log')
    _display_lock = threading.Lock()
    _student_lines: dict[str, str] = {}

    def _render() -> str:  # caller must hold _display_lock
        return "\n".join(_student_lines.values()) if _student_lines else ""

    def _mark_one_page(
        assignment: dict, p_label: int, answer_label: int, answer_page_count: int,
        extra_scan_pages: list[int],
    ) -> tuple[dict | None, dict | None]:
        student_name: str = assignment["student_name"]
        safe_name = student_name or f"Unknown_{p_label}"
        key = f"{student_name}_{p_label}"

        _total_pages = len(assignment["page_numbers"])
        with _display_lock:
            _student_lines[key] = (
                f"[dim]  {icon('info')}  Student '{student_name}'"
                f" · page {p_label}/{_total_pages}[/]"
            )
            if _use_live:
                live.update(_render())

        bp_path = artifact_blueprint_path(ctx.artifact_dir, answer_label, fmt=fmt.artifact_ext())
        blueprint_str = bp_path.read_text(encoding="utf-8")
        blueprint = fmt.deserialize_blueprint(blueprint_str)

        t0 = time.perf_counter()
        prompt_save = artifact_marking_prompt_path(ctx.artifact_dir, student_name, p_label, ctx.step_offset)
        try:
            _page_graphics = _scheme_graphics_for_page(blueprint, _graphics_map)
            _use_pdf_path = _provider == "gemini"
            if _use_pdf_path:
                import shutil
                import tempfile
                exercise_scan_page = assignment["page_numbers"][p_label - 1]
                all_scan_pages = [exercise_scan_page] + extra_scan_pages
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as _tmp:
                    tmp_path = _tmp.name
                try:
                    with fitz.open(str(ctx.cleaned_pdf)) as _src:
                        _out = fitz.open()
                        try:
                            for sp in all_scan_pages:
                                _out.insert_pdf(_src, from_page=sp - 1, to_page=sp - 1)
                            _out.save(tmp_path)
                        finally:
                            _out.close()
                    local_pdf = ctx.artifact_dir / f"14_upload_{safe_name}_{p_label}.pdf"
                    shutil.copy(tmp_path, local_pdf)
                    filled = _mark_page_pdf(
                        tmp_path, blueprint, blueprint_str,
                        prompt_save_path=prompt_save,
                        warn=_warn,
                        scheme_graphics=_page_graphics,
                        has_continuation=bool(extra_scan_pages),
                        fmt=fmt,
                    )
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            else:
                b64 = _b64_cache[(student_name, p_label)]
                _scan_to_plabel = {sp: i + 1 for i, sp in enumerate(assignment["page_numbers"])}
                extra_b64 = [
                    _b64_cache[(student_name, _scan_to_plabel[esp])]
                    for esp in extra_scan_pages
                    if esp in _scan_to_plabel and (student_name, _scan_to_plabel[esp]) in _b64_cache
                ]
                filled = _mark_page(
                    client, model_id, b64, blueprint, _thinking_kw,
                    blueprint_xml=blueprint_str,
                    use_stream=_use_stream,
                    prompt_save_path=prompt_save,
                    warn=_warn,
                    scheme_graphics=_page_graphics,
                    fmt=fmt,
                    extra_b64=extra_b64,
                )
        except MarkingFailure as mf:
            filled = blueprint.copy()
            filled["student_name"] = student_name
            failure = {
                "student": student_name, "page": p_label,
                "attempts": mf.attempts, "error": str(mf.last_exc),
                "raw_response": mf.last_raw or None,
            }
            out_path = artifact_marked_path(ctx.artifact_dir, safe_name, p_label, fmt=fmt.artifact_ext())
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(fmt.serialize_filled(filled), encoding="utf-8")
            artifact_marked_md_path(ctx.artifact_dir, safe_name, p_label).write_text(
                marked_to_md(filled), encoding="utf-8"
            )
            failed_path = artifact_marked_failed_path(ctx.artifact_dir, safe_name, p_label)
            failed_path.parent.mkdir(parents=True, exist_ok=True)
            failed_path.write_text(json.dumps(failure, indent=2, ensure_ascii=False), encoding="utf-8")
            with _display_lock:
                _student_lines[key] = (
                    f"[red]  {icon('warn')}  Student '{student_name}'"
                    f" · page {p_label}/{_total_pages}  ·  FAILED[/]"
                )
                if _use_live:
                    live.update(_render())
                else:
                    get_console().print(_student_lines[key])
            return None, failure

        mark_dur = round(time.perf_counter() - t0, 2)
        if _page_graphics:
            _graphic_labels = [f"ms p{pg} Q{qn}" for qn, pg, _ in _page_graphics]
            _graphic_note = f"  · +graphic ({', '.join(_graphic_labels)})"
        else:
            _graphic_note = ""
        with _display_lock:
            _student_lines[key] = (
                f"[green]  {icon('ok')}  Student '{student_name}'"
                f" · page {p_label}/{_total_pages}  ·  {format_duration(mark_dur)}{_graphic_note}[/]"
            )
            if _use_live:
                live.update(_render())
            else:
                get_console().print(_student_lines[key])

        filled["student_name"] = student_name
        out_path = artifact_marked_path(ctx.artifact_dir, safe_name, p_label, fmt=fmt.artifact_ext())
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(fmt.serialize_filled(filled), encoding="utf-8")
        artifact_marked_md_path(ctx.artifact_dir, safe_name, p_label).write_text(
            marked_to_md(filled), encoding="utf-8"
        )
        return {"phase": "marking", "student": student_name, "page": p_label,
                "duration_s": mark_dur}, None

    all_failures: list[dict] = []
    _live_ctx = Live("", console=get_console(), refresh_per_second=4) if _use_live else contextlib.nullcontext()
    with _live_ctx as live:
        def _warn(msg: str) -> None:
            if _use_live:
                with _display_lock:
                    live.console.print(f"[yellow]  {icon('warn')}  {msg}[/]")
            else:
                warn_line(msg)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_mark_one_page, a, p_label, ans_lbl, ans_cnt, extras): (a["student_name"], p_label)
                for a, p_label, ans_lbl, ans_cnt, extras in page_tasks
            }
            for fut in as_completed(futures):
                try:
                    timing, failure = fut.result()
                except Exception as exc:  # noqa: BLE001
                    student, page = futures[fut]
                    failure = {
                        "student": student, "page": page,
                        "attempts": 1, "error": f"Unhandled worker exception: {exc}",
                        "raw_response": None,
                    }
                    timing = None
                    _warn(f"Unhandled exception for '{student}' page {page}: {exc}")
                with timings_lock:
                    if timing:
                        api_call_timings.append(timing)
                    if failure:
                        all_failures.append(failure)

    ctx.marking_failures = all_failures
    return api_call_timings
