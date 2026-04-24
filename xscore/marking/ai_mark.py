"""Step 14 — AI marking: iterate over student scan pages and fill blueprint JSONs.

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

import xml.etree.ElementTree as ET

from xscore.config import GEMINI_MAX_OUTPUT_TOKENS, MARKING_MODEL_DEFAULT, MAX_RETRIES
from xscore.marking.blueprints import marked_to_md
from xscore.shared.exam_paths import artifact_blueprint_xml_path, artifact_marked_failed_path, artifact_marked_md_path, artifact_marked_xml_path, artifact_prompt_path
from xscore.shared.prompt_logger import save_prompt
from xscore.shared.terminal_ui import format_duration, get_console, icon, info_line, ok_line, warn_line


from xscore.marking.mark_xml import (
    MarkingFailure, _blueprint_xml_to_dict, _parse_xml_response, filled_to_xml,
)
from xscore.marking.mark_page import _bq_key, _fix_mc_marks, _mark_page, _render_page_b64

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

    Reads 8_exam_student_list.json directly (same source as run_ai_marking).
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
    blueprint_xml: str,
    prompt_save_path: Path | None,
    warn: Callable[[str], None],
) -> dict:
    """Upload a pre-built multi-page PDF to Gemini and mark it.

    pdf_path is a temporary file built by the caller (exercise page + blank continuation pages).
    Raises MarkingFailure if all retries are exhausted.
    """
    import os
    from google.genai import types as gai_types
    from xscore.shared.prompt_logger import save_response
    from eXercise.ai_client import make_gemini_native_client, parse_model_effort

    gai_client = make_gemini_native_client()
    if gai_client is None:
        raise RuntimeError("GEMINI_API_KEY not set — cannot upload multi-page PDF for blank continuation pages")

    _model_env = os.environ.get("MARKING_MODEL", "")
    model_id, _ = parse_model_effort(_model_env) if _model_env else ("gemini-2.5-flash", None)

    system_prompt = (
        "You are marking a student's exam answer. The uploaded PDF contains the exercise page "
        "followed by one or more continuation pages the student used for additional writing. "
        "Mark all pages together as one answer."
    )
    user_text = (
        "Fill in the three empty fields for each question "
        "(<student_answer>, <assigned_marks>, <explanation>):\n"
        f"{blueprint_xml}"
    )
    save_prompt(prompt_save_path, model=model_id, messages=[{"role": "user", "content": user_text}])

    _last_exc: BaseException = RuntimeError("no attempts made")
    _last_raw: str = ""
    _actual_attempts = 0
    uploaded = None
    for attempt in range(MAX_RETRIES + 1):
        _actual_attempts += 1
        try:
            if uploaded is None:
                uploaded = gai_client.files.upload(
                    file=pdf_path,
                    config=gai_types.UploadFileConfig(mime_type="application/pdf"),
                )
            resp = gai_client.models.generate_content(
                model=model_id,
                contents=[
                    gai_types.Part.from_uri(file_uri=uploaded.uri, mime_type="application/pdf"),
                    gai_types.Part.from_text(text=user_text),
                ],
                config=gai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    max_output_tokens=GEMINI_MAX_OUTPUT_TOKENS,
                ),
            )
            raw = resp.text or ""
            _last_raw = raw
            save_response(prompt_save_path, raw)
            parsed_questions = _parse_xml_response(raw)
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
            try:
                gai_client.files.delete(name=uploaded.name)
            except Exception as _del_exc:  # noqa: BLE001
                warn(f"Gemini file cleanup failed (file may remain in storage): {_del_exc}")
            return result
        except ET.ParseError as exc:
            warn("Marking XML parse error (PDF upload path) — XML repair failed, marking aborted")
            _last_exc = exc
            break
        except KeyboardInterrupt:
            raise
        except Exception as exc:  # noqa: BLE001
            warn(f"Gemini error (attempt {_actual_attempts}): {exc}")
            _last_exc = exc
            if attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
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

    Reads page assignments from ``8_exam_student_list.json`` (written by step 8)
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

    result = make_ai_client(model_env="MARKING_MODEL", default_model=_DEFAULT_MARKING_MODEL)
    if result is None:
        raise RuntimeError(
            "MARKING_MODEL client could not be created — check DASHSCOPE_API_KEY in .env"
        )
    client, model_id, _provider, _effort = result
    _use_stream, _thinking_kw = build_thinking_kwargs(_provider, _effort)

    # Load page assignments produced by step 8 name detection.
    list_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
    if not list_path.exists():
        raise FileNotFoundError(
            f"8_exam_student_list.json not found at {list_path} — run step 8 first"
        )
    raw_assignments: list[dict] = json.loads(list_path.read_text(encoding="utf-8"))
    # Each entry: {"student_name": str, "page_numbers": [int, ...], "confidence": str}

    # Load blank page detection results (written by step 8 blank_page_detection).
    _blank_json = ctx.artifact_dir / "8_blank_pages.json"
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
    _graphics_dir = ctx.artifact_dir / "11_mark_scheme_graphics"
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

        blueprint_xml = artifact_blueprint_xml_path(ctx.artifact_dir, answer_label).read_text(
            encoding="utf-8"
        )
        blueprint = _blueprint_xml_to_dict(blueprint_xml)

        t0 = time.perf_counter()
        prompt_save = artifact_prompt_path(ctx.artifact_dir, f"14_marked_{safe_name}_{p_label}")
        try:
            _page_graphics: list = []
            _use_pdf_path = extra_scan_pages and (
                os.environ.get("GEMINI_API_KEY", "").strip()
                or os.environ.get("GOOGLE_API_KEY", "").strip()
            )
            if extra_scan_pages and not _use_pdf_path:
                _warn(
                    f"GEMINI_API_KEY not set — blank continuation pages for "
                    f"'{student_name}' page {p_label} will be omitted from marking"
                )
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
                        tmp_path, blueprint, blueprint_xml,
                        prompt_save_path=prompt_save,
                        warn=_warn,
                    )
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            else:
                b64 = _b64_cache[(student_name, p_label)]
                _page_graphics = _scheme_graphics_for_page(blueprint, _graphics_map)
                filled = _mark_page(
                    client, model_id, b64, blueprint, _thinking_kw,
                    blueprint_xml=blueprint_xml,
                    use_stream=_use_stream,
                    prompt_save_path=prompt_save,
                    warn=_warn,
                    scheme_graphics=_page_graphics,
                )
        except MarkingFailure as mf:
            filled = blueprint.copy()
            filled["student_name"] = student_name
            failure = {
                "student": student_name, "page": p_label,
                "attempts": mf.attempts, "error": str(mf.last_exc),
                "raw_response": mf.last_raw or None,
            }
            out_xml = artifact_marked_xml_path(ctx.artifact_dir, safe_name, p_label)
            out_xml.parent.mkdir(parents=True, exist_ok=True)
            out_xml.write_text(filled_to_xml(filled), encoding="utf-8")
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
        out_xml = artifact_marked_xml_path(ctx.artifact_dir, safe_name, p_label)
        out_xml.parent.mkdir(parents=True, exist_ok=True)
        out_xml.write_text(filled_to_xml(filled), encoding="utf-8")
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
