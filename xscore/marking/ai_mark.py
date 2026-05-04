"""Step 29 — AI marking: iterate over student scan pages and fill blueprint JSONs.

Uses the MARKING_MODEL env var (default: qwen3.6-plus, off) via make_ai_client().
Requires DASHSCOPE_API_KEY to be set in .env.

Students are processed in parallel (MARKING_WORKERS workers, default 4).
Each worker opens its own fitz document handle (fitz is not thread-safe).

The "which scan pages does each AI call see?" question is now answered by
the marking page register (written by step 18, refined by step 21) — see
:mod:`xscore.marking.marking_page_register`. This step loads the most-refined
register available and iterates it; runtime filters (scaffold-bounds cap and
the ``--student`` cohort filter) are applied at iteration time.
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from collections.abc import Callable
from typing import Any

from xscore.config import (
    GEMINI_MAX_OUTPUT_TOKENS,
    MARKING_MODEL_DEFAULT,
    MARKING_THINKING_BOOST_MULTIPLIER,
    MARKING_THINKING_BOOST_THRESHOLD,
)
from xscore.marking.blueprints import marked_to_md
from xscore.marking.formats import get_marking_format
from xscore.marking.formats.base import FormatParseError
from xscore.shared.exam_paths import (
    artifact_blueprint_path,
    artifact_cross_page_refs_json_path,
    artifact_mark_scheme_graphics_dir,
    artifact_marked_failed_path,
    artifact_marked_md_path,
    artifact_marked_path,
    artifact_marking_prompt_path,
    artifact_parent_refs_json_path,
    artifact_scheme_graphic_transcriptions_path,
)
from xscore.shared.prompt_logger import save_prompt
from xscore.shared.terminal_ui import format_duration, get_console, icon, info_line, warn_line

from xscore.marking.formats.base import MarkingFailure
from xscore.marking.mark_page import (
    _build_marking_system_prompt, _mark_page, _render_page_b64,
)
from xscore.marking.extract_answers import (
    _safe_load_json, load_student_answers, patch_blueprint_with_answers,
)

_DEFAULT_MARKING_MODEL = MARKING_MODEL_DEFAULT



def render_pages_b64(
    cleaned_pdf: Path,
    artifact_dir: Path,
    dpi: int,
    workers: int,
    *,
    instruction: Any = None,
    cli_filter: list[str] | None = None,
    limit_students: int | None = None,
) -> dict[tuple[str, int], str]:
    """Render all scan pages to base64 JPEG, parallelised.

    Reads 15_student_names/exam_student_list.json directly (same source as run_ai_marking).
    Each worker opens its own fitz.Document — fitz is not thread-safe.

    Both the prompt-derived ``instruction.student_filter`` and the
    ``--student`` CLI cohort filter are applied so the rendered set matches
    what the marker will actually consume. ``--limit-students N`` (passed as
    ``limit_students``) further slices to the first N after both filters.

    Returns {(student_name, page_label): b64_str}.
    """
    import fitz
    from concurrent.futures import as_completed
    from xscore.shared.exam_paths import artifact_exam_student_list_json_path

    list_path = artifact_exam_student_list_json_path(artifact_dir)
    raw: list[dict] = _safe_load_json(list_path)

    if instruction is not None:
        sf = instruction.student_filter
        if sf.mode == "specific" and sf.names:
            raw = [a for a in raw if a["student_name"] in sf.names]
        elif sf.mode == "first_n" and sf.n:
            raw = raw[: sf.n]

    if cli_filter:
        wanted = {n.strip().lower() for n in cli_filter}
        raw = [
            a for a in raw
            if (a.get("student_name") or "").strip().lower() in wanted
        ]

    if limit_students:
        raw = raw[:limit_students]

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
    scheme_graphics: "list[tuple[str, int, str, str]]" = (),
    has_continuation: bool = False,
    fmt=None,
    is_cs: bool = False,
    has_student_answers: bool = False,
    should_cache: bool = False,
    is_all_mcq: bool = False,
) -> tuple[dict, list[dict]]:
    """Upload a PDF page (+ optional continuation pages) to Gemini and mark it.

    Returns ``(result, mcq_corrections)`` — same shape as :func:`_mark_page`.

    Raises MarkingFailure if all retries are exhausted.
    *has_student_answers* — kept for backward compat; ignored. The marking
    AI always treats `transcribed_answer` as read-only input (step 28 always
    runs first). See ``_mark_page`` docstring.

    *scheme_graphics* — 4-tuples (qnum, ms_page, b64_png, transcription). The
    PDF path attaches the image bytes alongside the user text and embeds the
    transcription via the system prompt.

    *is_all_mcq* — when True, swap in the short ``ai_marking_mcq`` prompt;
    same semantics as :func:`_mark_page`.
    """
    import os
    from google.genai import types as gai_types
    from xscore.shared.prompt_logger import save_response
    from eXercise.ai_client import (
        build_gemini_thinking_config,
        gemini_pdf_part,
        make_gemini_native_client,
        parse_model_spec,
        split_gemini_response,
    )
    from eXercise.api_retry import retry_api_call

    if fmt is None:
        from xscore.marking.formats.base import MarkingFormat
        fmt = MarkingFormat()

    gai_client = make_gemini_native_client(should_cache=should_cache)
    if gai_client is None:
        raise RuntimeError("GEMINI_API_KEY not set — required for Gemini MARKING_MODEL")

    _model_env = os.environ.get("MARKING_MODEL", "")
    if _model_env:
        model_id, _thinking, _max_tok = parse_model_spec(_model_env)
    else:
        model_id, _thinking, _max_tok = ("gemini-2.5-flash", None, None)

    system_prompt = _build_marking_system_prompt(
        blueprint, scheme_graphics, has_continuation=has_continuation, fmt=fmt, is_cs=is_cs,
        has_student_answers=has_student_answers, is_all_mcq=is_all_mcq,
    )
    from xscore.prompts.loader import load_prompt
    from xscore.marking.mark_page import _rename_blueprint_for_prompt
    _user_prompt_name = "ai_marking_mcq" if is_all_mcq else fmt.prompt_name()
    _, user_text = load_prompt(
        _user_prompt_name, section="user",
        blueprint=_rename_blueprint_for_prompt(blueprint_str),
    )
    _pdf_b64 = base64.b64encode(Path(pdf_path).read_bytes()).decode()
    _logged_user: list[dict] = [
        {"type": "image_url",
         "image_url": {"url": f"data:application/pdf;base64,{_pdf_b64}"}},
        {"type": "text", "text": user_text},
    ]
    for _qn, _ms_page, _g_b64, _ in scheme_graphics:
        _logged_user.append(
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{_g_b64}"}}
        )
    save_prompt(
        prompt_save_path, model=model_id, system=system_prompt,
        messages=[{"role": "user", "content": _logged_user}],
    )
    from xscore.shared.prompt_logger import save_input_data
    save_input_data(prompt_save_path, blueprint_str, ext="yaml")

    cfg: dict = {
        "system_instruction": system_prompt,
        "max_output_tokens": _max_tok or GEMINI_MAX_OUTPUT_TOKENS,
    }
    cfg.update(fmt.api_extra_kwargs(model_id))
    if _thinking is not None:
        cfg["thinking_config"] = build_gemini_thinking_config(_thinking)
    config = gai_types.GenerateContentConfig(**cfg)

    pdf_part = gemini_pdf_part(gai_client, pdf_path, label="marking")

    def _do_call() -> tuple[str, str]:
        contents = [
            pdf_part,
            gai_types.Part.from_text(text=user_text),
        ]
        for _qn, _ms_page, g_b64, _ in scheme_graphics:
            contents.append(
                gai_types.Part.from_bytes(
                    data=base64.b64decode(g_b64), mime_type="image/png"
                )
            )
        _resp = gai_client.models.generate_content(
            model=model_id, contents=contents, config=config,
        )
        return split_gemini_response(_resp)  # (answer_text, thinking_text)

    _last_raw: str = ""
    try:
        raw, thinking_text = retry_api_call(_do_call, label="Marking PDF")
        _last_raw = raw
        save_response(prompt_save_path, raw, thinking=thinking_text)
        from xscore.marking.mark_page import _apply_marking_response, _finalize_marking
        # PDF upload path runs a single call with no completeness retry — the
        # retry helper in mark_page.py is wired for chat.completions only.
        # Mirrors the JPEG path's apply→warn→finalize sequence otherwise.
        result, unfilled, unmatched, mcq_corrections = _apply_marking_response(raw, blueprint, fmt)
        if unmatched:
            warn(f"Marking: AI returned question(s) with no blueprint match: {unmatched}")
        if unfilled:
            warn(f"Marking: {len(unfilled)} blueprint question(s) skipped by AI: {unfilled}")
        _finalize_marking(result, warn)
        # The canonical marked YAML is written by run_ai_marking() under
        # 29_ai_marking/students/<S>/page_N.yaml; the prompt-logger sidecar
        # would only duplicate the same content with student_name='', so skip it.
        return result, mcq_corrections
    except FormatParseError as exc:
        warn(f"Marking parse error (PDF upload path) — marking aborted ({exc})")
        raise MarkingFailure(attempts=1, last_exc=exc, last_raw=_last_raw)
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        raise MarkingFailure(attempts=1, last_exc=exc, last_raw=_last_raw)


def _scheme_graphics_for_page(
    blueprint: dict,
    graphics_map: dict[str, list[Path]],
    b64_cache: dict[Path, str] | None = None,
    transcriptions: dict[Path, str] | None = None,
) -> list[tuple[str, int, str, str]]:
    """Return (question_number, ms_page, base64_png, transcription) tuples for
    mark-scheme graphics on this page.

    *b64_cache* is a pre-computed ``{path: base64}`` map so PNG read+encode happens
    once per file rather than once per (student × page) call. Falls back to live
    read+encode for paths not in the cache (or when cache is None).

    *transcriptions* maps PNG path → textual description (from step 25). PNGs
    without a transcription get an empty string, which makes the GRAPHICS prompt
    section render exactly as it did before step 25 existed.
    """
    out = []
    for q in blueprint.get("questions", []):
        qnum = str(q.get("number", ""))
        safe_num = re.sub(r"[^\w]", "_", qnum)
        for png_path in graphics_map.get(safe_num, []):
            page_prefix = png_path.name.split("_")[0]
            ms_page = int(page_prefix) if page_prefix.isdigit() else 0
            if b64_cache is not None and png_path in b64_cache:
                b64 = b64_cache[png_path]
            else:
                b64 = base64.b64encode(png_path.read_bytes()).decode()
            transcription = (transcriptions or {}).get(png_path, "")
            out.append((qnum, ms_page, b64, transcription))
    return out


def run_ai_marking(ctx: Any, *, dpi: int | None = None) -> list[dict]:
    """Run the full AI marking loop for all students and pages.

    Reads page assignments from ``15_student_names/exam_student_list.json``
    (written by step 15) so each student's scan pages are determined by name
    detection, not position. The list of (student, answer_label, scan_pages)
    triples to mark is loaded from the marking page register written by
    step 18 (and refined by step 21). Pages are processed in parallel
    (MARKING_WORKERS env var, default varies with cpu_count). *dpi* defaults
    to ``MARKING_DPI`` when not supplied. Returns a list of API call timing
    records.
    """
    from xscore.config import MARKING_DPI
    if dpi is None:
        dpi = MARKING_DPI

    import fitz

    from eXercise.ai_client import make_ai_client, build_completion_kwargs
    from xscore.marking.marking_page_register import (
        _pretty_source_label,
        build_initial_register,
        iter_marking_calls,
        load_register,
        print_register_summary,
    )
    from xscore.shared.exam_paths import artifact_exam_student_list_json_path

    fmt = get_marking_format()

    # Gate the CODE_FORMATTING prompt section on the detected subject. Reads
    # ctx.subject set by detect_subject (rehydrated from artifact on resume).
    from xscore.shared.subjects import needs_code_formatting
    _is_cs = needs_code_formatting(ctx)

    result = make_ai_client(model_env="MARKING_MODEL", default_model=_DEFAULT_MARKING_MODEL)
    if result is None:
        raise RuntimeError(
            "MARKING_MODEL client could not be created — "
            "check DASHSCOPE_API_KEY / GEMINI_API_KEY in .env"
        )
    client, model_id, _provider, _thinking, max_tok = result
    _use_stream, _thinking_kw = build_completion_kwargs(_provider, _thinking, max_tok)

    # Resolve the response-cache opt-in once. The user enables it by including
    # "reuse cache" in the natural-language prompt (parsed in step 1, sets
    # ctx.instruction.reuse_cache). XSCORE_REUSE_CACHE=1 is also honoured for
    # ad-hoc testing without re-issuing the prompt.
    from xscore.shared.response_cache import reuse_cache_enabled
    _reuse_cache_active = reuse_cache_enabled(ctx)
    if _reuse_cache_active:
        info_line("Response cache enabled · all xscore AI calls will check ~/.cache/xscore/responses/")

    # Load page assignments produced by step 15 name detection. The register
    # already encodes most of the per-call data, but we need the original
    # assignment dict (with confidence + raw page_numbers) for downstream
    # code that consumes the page_tasks tuples.
    list_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
    if not list_path.exists():
        raise FileNotFoundError(
            f"15_student_names/exam_student_list.json not found at {list_path} — "
            "run step 15 first"
        )
    raw_assignments: list[dict] = _safe_load_json(list_path)

    _instr = getattr(ctx, "instruction", None)
    _unfiltered_student_count = len(raw_assignments)
    if _instr is not None:
        sf = _instr.student_filter
        if sf.mode == "specific" and sf.names:
            raw_assignments = [a for a in raw_assignments if a["student_name"] in sf.names]
        elif sf.mode == "first_n" and sf.n:
            raw_assignments = raw_assignments[: sf.n]

    # CLI-driven --student filter (case-insensitive exact match on student_name).
    # Layered AFTER the NL-prompt student_filter so both narrow the cohort.
    cli_filter = getattr(ctx, "student_filter", None)
    if cli_filter:
        wanted = {n.strip().lower() for n in cli_filter}
        before = len(raw_assignments)
        raw_assignments = [
            a for a in raw_assignments
            if (a.get("student_name") or "").strip().lower() in wanted
        ]
        if not raw_assignments:
            warn_line(
                f"--student filter {sorted(wanted)} matched 0 of {before} students — "
                f"nothing to mark; aborting step 29."
            )
            raise SystemExit(2)
        kept = [a["student_name"] for a in raw_assignments]
        info_line(f"--student filter active · marking {len(kept)} of {before}: {', '.join(kept)}")

    # CLI-driven --limit-students slice. Applied last so it composes with
    # both the NL-prompt filter and the --student filter.
    limit_students = getattr(ctx, "limit_students", None)
    if limit_students:
        before = len(raw_assignments)
        raw_assignments = raw_assignments[:limit_students]
        kept = [a["student_name"] for a in raw_assignments]
        info_line(f"--limit-students active · marking {len(kept)} of {before}: {', '.join(kept)}")

    _default_workers = min(os.cpu_count() or 4, 16)
    try:
        workers = int(os.environ.get("MARKING_WORKERS", str(_default_workers)))
    except ValueError:
        warn_line(
            f"MARKING_WORKERS={os.environ.get('MARKING_WORKERS')!r} is not an "
            f"integer — falling back to default {_default_workers}."
        )
        workers = _default_workers
    timings_lock = threading.Lock()
    api_call_timings: list[dict] = []

    def _render_inline() -> dict[tuple[str, int], str]:
        _total_pages = sum(len(a["page_numbers"]) for a in raw_assignments)
        info_line(f"Rendering {_total_pages} page(s) for {len(raw_assignments)} students at {dpi} DPI …")
        return render_pages_b64(
            ctx.cleaned_pdf, ctx.artifact_dir, dpi, workers,
            instruction=getattr(ctx, "instruction", None),
            cli_filter=getattr(ctx, "student_filter", None),
            limit_students=getattr(ctx, "limit_students", None),
        )

    b64_future = getattr(ctx, "b64_future", None)
    if b64_future is not None:
        try:
            _b64_cache = b64_future.result()  # callback already announced success/failure
        except Exception:  # noqa: BLE001 — callback already warned; fall back to inline
            _b64_cache = _render_inline()
    else:
        _b64_cache = _render_inline()

    # Pre-build mark-scheme graphics map: safe_qnum → sorted list of PNG paths.
    # Also pre-encode each PNG to base64 once (reused across all student×page calls
    # via _scheme_graphics_for_page's b64_cache parameter).
    _graphics_dir = artifact_mark_scheme_graphics_dir(ctx.artifact_dir)
    _graphics_map: dict[str, list[Path]] = {}
    _graphics_b64_cache: dict[Path, str] = {}
    if _graphics_dir.is_dir():
        _gfx_re = re.compile(r"^\d+_(.+)_(\d+)\.png$")
        for _p in sorted(_graphics_dir.glob("*.png")):
            _m = _gfx_re.match(_p.name)
            if _m:
                _graphics_map.setdefault(_m.group(1), []).append(_p)
                _graphics_b64_cache[_p] = base64.b64encode(_p.read_bytes()).decode()
        for _v in _graphics_map.values():
            _v.sort()

    # Pre-load step-25 transcriptions (PNG path → description string). Missing
    # or malformed file degrades silently to {} so marking still runs against
    # the raw images alone.
    _transcriptions_path = artifact_scheme_graphic_transcriptions_path(ctx.artifact_dir)
    _transcriptions_by_path: dict[Path, str] = {}
    if _transcriptions_path.exists():
        try:
            import yaml as _yaml
            _t_doc = _yaml.safe_load(_transcriptions_path.read_text(encoding="utf-8")) or {}
            for _entry in _t_doc.get("graphics", []) or []:
                if not isinstance(_entry, dict):
                    continue
                _fname = str(_entry.get("file") or "")
                _t = str(_entry.get("transcription") or "").strip()
                if _fname and _t:
                    _transcriptions_by_path[_graphics_dir / _fname] = _t
        except Exception:  # noqa: BLE001 — degraded mode is fine
            pass

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

    # Load the marking page register. Step 21 (cross-page figures) refines
    # what step 18 wrote; load_register tries the most-refined first. If
    # neither file exists (e.g. resuming a pre-renumber run), fall back to
    # building it in memory — same builder step 18 uses, so the result is
    # equivalent to a fresh v1 register.
    register = load_register(ctx.artifact_dir)
    if register is None:
        register = build_initial_register(ctx)

    # Step-21 diagnostics — used by the per-call line to render +context labels.
    # Missing files map to empty lists; _pretty_source_label degrades to a "?"
    # page placeholder rather than raising.
    def _load_refs(path: Path) -> list[dict]:
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []
    _figure_refs = _load_refs(artifact_cross_page_refs_json_path(ctx.artifact_dir))
    _parent_refs = _load_refs(artifact_parent_refs_json_path(ctx.artifact_dir))

    # Pre-marking summary table — render once, before the per-page loop kicks in.
    # The register already encodes the cover-page skip, handwriting-extras, and
    # cross-page-figure extras. iter_marking_calls additionally applies the
    # runtime scaffold-bounds cap and (implicitly via the filtered
    # raw_assignments) the cohort filter.
    _scaffold_pc = ctx.scaffold.page_count if ctx.scaffold is not None else None
    _student_filter_set = {a["student_name"] for a in raw_assignments}
    page_tasks: list[tuple[dict, int, int, int, list[int], list[str]]] = list(
        iter_marking_calls(
            register,
            raw_assignments=raw_assignments,
            scaffold_page_count=_scaffold_pc,
            artifact_dir=ctx.artifact_dir,
            fmt_ext=fmt.artifact_ext(),
        )
    )
    _filtered_call_count = len(page_tasks)
    _filtered_page_image_count = sum(1 + len(t[4]) for t in page_tasks)
    print_register_summary(
        register,
        filtered_call_count=_filtered_call_count,
        filtered_student_count=len(_student_filter_set),
        filtered_page_image_count=_filtered_page_image_count,
    )

    import contextlib
    import sys
    from rich.live import Live

    _use_live = sys.stdout.isatty() and not hasattr(sys.stdout, '_log')
    _display_lock = threading.Lock()
    _student_lines: dict[str, str] = {}

    # Column widths for the per-page progress lines (computed once so the
    # name / page / duration columns line up across rows).
    _max_student_width = max(
        (len(a["student_name"]) for a in raw_assignments), default=1,
    )
    _max_total_pages = max(
        (len(a["page_numbers"]) for a in raw_assignments), default=1,
    )
    _page_label_width = len(str(_max_total_pages))

    def _render() -> str:  # caller must hold _display_lock
        return "\n".join(_student_lines.values()) if _student_lines else ""

    # Non-live mode: streaming reorder buffer so completion-order workers
    # still emit lines in submission (= sorted student × page) order.
    _print_buffer: dict[int, str] = {}
    _print_next: list[int] = [0]
    _print_lock = threading.Lock()

    def _emit_ordered(idx: int, line: str) -> None:
        with _print_lock:
            _print_buffer[idx] = line
            while _print_next[0] in _print_buffer:
                ln = _print_buffer.pop(_print_next[0])
                get_console().print(ln)
                _print_next[0] += 1

    def _mark_one_page(
        idx: int,
        assignment: dict, p_label: int, answer_label: int, answer_page_count: int,
        extra_scan_pages: list[int],
        extra_sources: list[str],
    ) -> tuple[dict | None, dict | None, list[dict]]:
        student_name: str = assignment["student_name"]
        safe_name = student_name or f"Unknown_{p_label}"
        key = f"{student_name}_{p_label}"

        _total_pages = len(assignment["page_numbers"])
        with _display_lock:
            _student_lines[key] = (
                f"[dim]  {icon('info')}  {student_name.ljust(_max_student_width)}"
                f"   page {p_label:>{_page_label_width}d} / {_total_pages:>{_page_label_width}d}[/]"
            )
            if _use_live:
                live.update(_render())

        bp_path = artifact_blueprint_path(ctx.artifact_dir, answer_label, fmt=fmt.artifact_ext())
        blueprint_str = bp_path.read_text(encoding="utf-8")
        blueprint = fmt.deserialize_blueprint(blueprint_str)

        from xscore.marking.blueprints import is_all_mcq_page
        _is_all_mcq = is_all_mcq_page(blueprint.get("questions") or [])

        # Pre-fill student_answer from step 26 (extract_student_answers) when
        # the per-(student, page) artifact exists. Soft fallback: a missing
        # extraction artifact (e.g. step 26 had a failure for this page)
        # leaves student_answer empty and the AI transcribes during marking
        # as it did pre-refactor.
        _answers_map = load_student_answers(ctx.artifact_dir, student_name, p_label)
        _has_student_answers = bool(_answers_map)
        if _has_student_answers:
            for bq in blueprint.get("questions", []):
                qnum = str(bq.get("number", ""))
                if qnum in _answers_map:
                    bq["student_answer"] = _answers_map[qnum]
            # Patch the on-disk blueprint string in place — preserves the
            # original format (xml/yaml/json) and structure exactly, mutating
            # only the student_answer fields. The AI sees pre-filled values
            # in the prompt.
            blueprint_str = patch_blueprint_with_answers(blueprint_str, _answers_map, fmt)

        t0 = time.perf_counter()
        prompt_save = artifact_marking_prompt_path(ctx.artifact_dir, student_name, p_label)
        try:
            _page_graphics = _scheme_graphics_for_page(
                blueprint, _graphics_map, _graphics_b64_cache,
                transcriptions=_transcriptions_by_path,
            )
            _use_pdf_path = _provider == "gemini"
            if _use_pdf_path:
                import tempfile
                exercise_scan_page = assignment["page_numbers"][p_label - 1]
                # Sort ascending so the AI sees pages in natural reading order
                # (parent stems / referenced figures land before the child page).
                all_scan_pages = sorted([exercise_scan_page] + extra_scan_pages)
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
                    filled, _page_corrections = _mark_page_pdf(
                        tmp_path, blueprint, blueprint_str,
                        prompt_save_path=prompt_save,
                        warn=_warn,
                        scheme_graphics=_page_graphics,
                        has_continuation=bool(extra_scan_pages),
                        fmt=fmt,
                        is_cs=_is_cs,
                        has_student_answers=_has_student_answers,
                        should_cache=_reuse_cache_active,
                        is_all_mcq=_is_all_mcq,
                    )
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            else:
                # Sort ascending so the bundle reads top-to-bottom in scan-page
                # order; primary slot just carries whichever page is earliest.
                exercise_scan_page = assignment["page_numbers"][p_label - 1]
                _scan_to_plabel = {sp: i + 1 for i, sp in enumerate(assignment["page_numbers"])}
                _all_pages = sorted([exercise_scan_page] + extra_scan_pages)
                _all_b64 = [
                    _b64_cache[(student_name, _scan_to_plabel[sp])]
                    for sp in _all_pages
                    if sp in _scan_to_plabel and (student_name, _scan_to_plabel[sp]) in _b64_cache
                ]
                if not _all_b64:
                    raise MarkingFailure(
                        attempts=0,
                        last_exc=RuntimeError(
                            f"No rendered page images available for "
                            f"{student_name!r} page label {p_label} "
                            f"(scan pages {_all_pages})"
                        ),
                    )
                b64 = _all_b64[0]
                extra_b64 = _all_b64[1:]
                # Boost thinking budget on pages with hard questions. Long-answer
                # questions (≥ THRESHOLD marks) tend to exhaust the base budget
                # and force the model into minimum-viable output, which loses
                # structural fidelity (e.g. dropping the YAML wrapper).
                _max_marks_on_page = max(
                    (int(q.get("max_marks") or 0) for q in (blueprint.get("questions") or [])),
                    default=0,
                )
                if _thinking and _max_marks_on_page >= MARKING_THINKING_BOOST_THRESHOLD \
                        and MARKING_THINKING_BOOST_MULTIPLIER != 1:
                    _boosted = int(_thinking * MARKING_THINKING_BOOST_MULTIPLIER)
                    _use_stream_call, _thinking_kw_call = build_completion_kwargs(
                        _provider, _boosted, max_tok,
                    )
                    info_line(
                        f"Marking p{p_label} ({student_name}): boosting thinking budget "
                        f"{_thinking} → {_boosted} (max question = {_max_marks_on_page} marks, "
                        f"threshold = {MARKING_THINKING_BOOST_THRESHOLD}, "
                        f"×{MARKING_THINKING_BOOST_MULTIPLIER})"
                    )
                else:
                    _use_stream_call = _use_stream
                    _thinking_kw_call = _thinking_kw
                filled, _page_corrections = _mark_page(
                    client, model_id, b64, blueprint, _thinking_kw_call,
                    blueprint_xml=blueprint_str,
                    use_stream=_use_stream_call,
                    prompt_save_path=prompt_save,
                    warn=_warn,
                    scheme_graphics=_page_graphics,
                    fmt=fmt,
                    extra_b64=extra_b64,
                    reuse_cache=_reuse_cache_active,
                    is_cs=_is_cs,
                    has_student_answers=_has_student_answers,
                    is_all_mcq=_is_all_mcq,
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
                    f"[red]  {icon('warn')}  {student_name.ljust(_max_student_width)}"
                    f"   page {p_label:>{_page_label_width}d} / {_total_pages:>{_page_label_width}d}"
                    f"   ·  FAILED[/]"
                )
                if _use_live:
                    live.update(_render())
            if not _use_live:
                _emit_ordered(idx, _student_lines[key])
            return None, failure, []

        mark_dur = round(time.perf_counter() - t0, 2)
        _extras: list[str] = []
        if _is_all_mcq:
            _extras.append("+mcq-only")
        _context_labels = [
            lab for src in extra_sources
            if (lab := _pretty_source_label(src, _figure_refs, _parent_refs, compact=True)) is not None
        ]
        if _context_labels:
            _extras.append(f"+context {', '.join(_context_labels)}")
        _n_cont = sum(1 for src in extra_sources if src == "continuation")
        if _n_cont:
            _extras.append(f"+continuation {_n_cont} page{'s' if _n_cont != 1 else ''}")
        if _page_graphics:
            _graphic_labels = [f"ms p{pg} Q{qn}" for qn, pg, _, _ in _page_graphics]
            _extras.append(f"+graphic {', '.join(_graphic_labels)}")
        _extras_text = ("   " + "   ".join(_extras)) if _extras else ""
        with _display_lock:
            _student_lines[key] = (
                f"[green]  {icon('ok')}  {student_name.ljust(_max_student_width)}"
                f"   page {p_label:>{_page_label_width}d} / {_total_pages:>{_page_label_width}d}"
                f"   ·  {format_duration(mark_dur).rjust(6)}"
                f"{_extras_text}[/]"
            )
            if _use_live:
                live.update(_render())
        if not _use_live:
            _emit_ordered(idx, _student_lines[key])

        filled["student_name"] = student_name
        out_path = artifact_marked_path(ctx.artifact_dir, safe_name, p_label, fmt=fmt.artifact_ext())
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(fmt.serialize_filled(filled), encoding="utf-8")
        artifact_marked_md_path(ctx.artifact_dir, safe_name, p_label).write_text(
            marked_to_md(filled), encoding="utf-8"
        )
        # Tag corrections with student/page for the run-level aggregator and
        # emit one info_line per correction so they're visible in the live log.
        # `page` is the per-student answer-page label (1..N for that student);
        # `scan_page` is the absolute page index in the cleaned scan PDF, which
        # is what shows up in the per-page artifact paths and is the right
        # reference when locating the student's actual handwriting.
        _scan_page = assignment["page_numbers"][p_label - 1]
        tagged_corrections: list[dict] = []
        for _c in _page_corrections:
            tagged_corrections.append({
                "student": student_name,
                "page": p_label,
                "scan_page": _scan_page,
                "number": _c.get("number"),
                "from": _c.get("from"),
                "to": _c.get("to"),
            })
            info_line(
                f"  MCQ correction: {student_name} ans p{p_label} (scan p{_scan_page}) "
                f"Q{_c.get('number')}: {_c.get('from')} → {_c.get('to')}"
            )
        return ({"phase": "marking", "student": student_name, "page": p_label,
                "duration_s": mark_dur}, None, tagged_corrections)

    all_failures: list[dict] = []
    all_corrections: list[dict] = []
    _live_ctx = Live("", console=get_console(), refresh_per_second=4) if _use_live else contextlib.nullcontext()
    with _live_ctx as live:
        def _warn(msg: str) -> None:
            if _use_live:
                with _display_lock:
                    live.console.print(f"[yellow]  {icon('warn')}  {msg}[/]")
            else:
                warn_line(msg)

        # Sort tasks by global scan page so the reorder buffer in non-live mode
        # produces lines in ascending scan-page order (which also groups each
        # student's pages together, since page_numbers are contiguous per student).
        page_tasks_sorted = sorted(
            page_tasks,
            key=lambda t: t[0]["page_numbers"][t[1] - 1],
        )
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {
                ex.submit(_mark_one_page, idx, a, p_label, ans_lbl, ans_cnt, extras, sources): (a["student_name"], p_label)
                for idx, (a, p_label, ans_lbl, ans_cnt, extras, sources) in enumerate(page_tasks_sorted)
            }
            for fut in as_completed(futures):
                try:
                    timing, failure, corrections = fut.result()
                except Exception as exc:  # noqa: BLE001
                    student, page = futures[fut]
                    failure = {
                        "student": student, "page": page,
                        "attempts": 1, "error": f"Unhandled worker exception: {exc}",
                        "raw_response": None,
                    }
                    timing = None
                    corrections = []
                    _warn(f"Unhandled exception for '{student}' page {page}: {exc}")
                with timings_lock:
                    if timing:
                        api_call_timings.append(timing)
                    if failure:
                        all_failures.append(failure)
                    if corrections:
                        all_corrections.extend(corrections)

    ctx.marking_failures = all_failures
    ctx.mcq_corrections = all_corrections
    return api_call_timings
