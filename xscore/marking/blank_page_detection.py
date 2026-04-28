"""Steps 14–15: detect blank pages in the empty exam, then check student scans for handwriting.

Step 14 (exam_blank_detection): text-only LLM call. Reads every page's extracted
text from the empty exam PDF and identifies which pages are blank (no question text,
only writing lines or "BLANK PAGE" heading). Writes
``14_exam_blank_detection/blank_exam_pages.json``.

Step 15 (student_handwriting_check): vision LLM call per (student × answer page).
Reads the step-14 artifact, renders the corresponding scan pages as JPEGs, and checks
for student handwriting. Under ``HANDWRITING_CHECK_WIDE=1`` (default) every
answer page is checked; under ``=0`` only step-14 blank pages are checked.
Writes ``15_student_handwriting/handwriting.json``.

Both functions emit per-page / per-task progress lines via the terminal_ui
``info_line`` / ``ok_line`` / ``warn_line`` helpers (mirrors step 12's
``_ocr_and_match`` idiom). Policy stays at the dispatcher: INCONCLUSIVE
returns from these functions; the dispatcher in ``xscore/steps/geometry.py``
decides warn-vs-SystemExit based on the per-step ``*_STRICT`` env var.

Pages where ``_has_handwriting`` could not be determined are **omitted** from
``blank_scan_pages`` and ``pages_without_handwriting`` (so the consumer in
``xscore/marking/marking_page_register.py`` is unaffected) and listed under a
sibling ``inconclusive_pages`` field per student.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from xscore.shared.models import PageAssignment


class BlankCheckStatus(Enum):
    PASSED = "PASSED"
    INCONCLUSIVE = "INCONCLUSIVE"


from eXercise.api_retry import retry_api_call


# ─────────── Text + image extraction ─────────────────────────────────────────

def _exam_page_texts(exam_pdf: Path) -> list[str]:
    import fitz
    with fitz.open(str(exam_pdf)) as doc:
        return [doc[i].get_text().strip() for i in range(doc.page_count)]


def _render_page_jpeg(pdf_path: Path, page_1based: int, dpi: int = 150) -> bytes:
    import fitz
    with fitz.open(str(pdf_path)) as doc:
        pix = doc[page_1based - 1].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    return pix.tobytes("jpeg")


# ─────────── Model client (shared by both helpers) ───────────────────────────

class _ClientState:
    def __init__(self, gai: Any, oa: Any, provider: str | None) -> None:
        self.gai = gai
        self.oa = oa
        self.provider = provider


def _build_client_state(model_id: str) -> _ClientState | str:
    """Return ``_ClientState`` on success, or a human-readable error message string."""
    if model_id.startswith("gemini"):
        from eXercise.ai_client import make_gemini_native_client
        gai = make_gemini_native_client()
        if gai is None:
            return "GEMINI_API_KEY not set"
        return _ClientState(gai=gai, oa=None, provider="gemini")
    from eXercise.ai_client import make_ai_client
    result = make_ai_client(model_env="", default_model=model_id)
    if result is None:
        return f"model={model_id} requires API key for its provider"
    oa, _, provider, _, _ = result
    return _ClientState(gai=None, oa=oa, provider=provider)


# ─────────── Response parsers ────────────────────────────────────────────────

def _parse_blank_pages(raw: str) -> set[int] | None:
    """Parse blank-page list. Returns ``set[int]`` on success (possibly empty),
    or ``None`` when the response is malformed / unusable.

    Accepts either Gemini ``[1, 2, 3]`` or OA ``{"blank_pages": [...]}`` shapes.
    An empty result list is *legitimate* (means "no blanks found") and returns
    ``set()``; only structural failures return ``None``.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("blank_pages")
        if pages is None:
            pages = data.get("pages")
        if pages is None:
            return None
    else:
        return None
    if not isinstance(pages, list):
        return None
    try:
        return {int(p) for p in pages}
    except (TypeError, ValueError):
        return None


def _parse_handwriting(raw: str) -> bool | None:
    """Parse handwriting yes/no response. Returns ``True``/``False`` or ``None``
    when the response is malformed."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if isinstance(data, bool):
        return data
    if isinstance(data, dict):
        v = data.get("answer")
        if v is None:
            v = data.get("has_handwriting")
        if isinstance(v, bool):
            return v
    return None


# ─────────── Step 14: find blank pages in empty exam ─────────────────────────

def find_blank_exam_pages(
    state: _ClientState,
    exam_texts: list[str],
    model_id: str,
    artifact_dir: Path | None,
    *,
    thinking_tokens: int | None = None,
    max_tokens: int | None = None,
) -> set[int] | None:
    """One LLM text call to identify blank exam pages.

    Returns ``set[int]`` of 1-based page numbers (possibly empty) on success;
    ``None`` when the call could not be completed or the response was malformed.

    The prompt includes the full candidate list so the model cannot hallucinate
    out-of-range numbers. The parsed result is additionally clipped to the valid
    set as a second layer of defence.
    """
    from xscore.shared.prompt_logger import save_prompt, save_response
    from xscore.shared.exam_paths import (
        artifact_blank_detection_txt_path,
        artifact_exam_blank_prompt_path,
    )
    from xscore.prompts.loader import load_prompt

    num_pages = len(exam_texts)
    candidates = list(range(1, num_pages + 1))

    page_lines: list[str] = []
    for i, text in enumerate(exam_texts, 1):
        page_lines += [f"Page {i}:", text or "(no printed text)", ""]
    exam_pages_block = "\n".join(page_lines)

    _, prompt = load_prompt(
        "exam_blank_detection",
        exam_pages_block=exam_pages_block,
        num_pages=num_pages,
        page_word="page" if num_pages == 1 else "pages",
        candidates=candidates,
    )
    prompt = prompt.rstrip("\n")

    if artifact_dir:
        det_path = artifact_blank_detection_txt_path(artifact_dir)
        det_path.parent.mkdir(parents=True, exist_ok=True)
        det_path.write_text(prompt, encoding="utf-8")

    save_path = (
        artifact_exam_blank_prompt_path(artifact_dir, "blank_detection_exam")
        if artifact_dir else None
    )
    save_prompt(save_path, model=model_id, messages=[{"role": "user", "content": prompt}])

    try:
        raw, thinking_text = retry_api_call(
            lambda: _call_blank_detection(state, prompt, model_id, thinking_tokens, max_tokens),
            label="Blank page detection (exam)",
        )
    except Exception:
        return None
    save_response(save_path, raw, thinking=thinking_text)

    result = _parse_blank_pages(raw)
    if result is None:
        return None
    valid = set(range(1, num_pages + 1))
    return result & valid


def _call_blank_detection(
    state: _ClientState,
    prompt: str,
    model_id: str,
    thinking_tokens: int | None,
    max_tokens: int | None,
) -> tuple[str, str]:
    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import build_gemini_thinking_config, split_gemini_response
        cfg_kwargs: dict = {
            "max_output_tokens": max_tokens or 256,
            "response_mime_type": "application/json",
            "response_schema": list[int],
        }
        if thinking_tokens is not None:
            cfg_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
        resp = state.gai.models.generate_content(
            model=model_id,
            contents=[gai_types.Part.from_text(text=prompt)],
            config=gai_types.GenerateContentConfig(**cfg_kwargs),
        )
        return split_gemini_response(resp)
    from eXercise.ai_client import build_completion_kwargs, collect_streamed_response
    use_stream, kw = build_completion_kwargs(state.provider, thinking_tokens, max_tokens or 256)
    oa_prompt = prompt + '\n\nReturn JSON only with this shape: {"blank_pages": [<int>, ...]}'
    msgs = [{"role": "user", "content": oa_prompt}]
    if use_stream:
        _th: list[str] = []
        stream = state.oa.chat.completions.create(model=model_id, messages=msgs, stream=True, **kw)
        return collect_streamed_response(stream, thinking_out=_th), "".join(_th)
    try:
        resp = state.oa.chat.completions.create(
            model=model_id, messages=msgs,
            response_format={"type": "json_object"}, **kw,
        )
    except Exception:  # noqa: BLE001
        resp = state.oa.chat.completions.create(model=model_id, messages=msgs, **kw)
    raw = resp.choices[0].message.content or ""
    thinking_text = getattr(resp.choices[0].message, "reasoning_content", "") or ""
    return raw, thinking_text


# ─────────── Step 15: per-page handwriting check ──────────────────────────────

def _has_handwriting(
    state: _ClientState,
    model_id: str,
    jpeg_bytes: bytes,
    save_path: Path | None,
) -> bool | None:
    """Vision call: does this blank scan page contain student handwriting?

    Returns ``True``/``False`` on success, or ``None`` when the call could
    not be completed or the response was malformed.
    """
    from xscore.shared.prompt_logger import save_prompt, save_response
    from xscore.prompts.loader import load_prompt

    _, prompt_text = load_prompt("student_handwriting_check")
    prompt_text = prompt_text.rstrip("\n")
    save_prompt(save_path, model=model_id, messages=[{"role": "user", "content": prompt_text}])

    try:
        raw, thinking_text = retry_api_call(
            lambda: _call_handwriting(state, prompt_text, model_id, jpeg_bytes),
            label="Handwriting check",
        )
    except Exception:
        return None
    save_response(save_path, raw, thinking=thinking_text)
    return _parse_handwriting(raw)


def _call_handwriting(
    state: _ClientState,
    prompt_text: str,
    model_id: str,
    jpeg_bytes: bytes,
) -> tuple[str, str]:
    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import split_gemini_response
        resp = state.gai.models.generate_content(
            model=model_id,
            contents=[
                gai_types.Part.from_bytes(data=jpeg_bytes, mime_type="image/jpeg"),
                gai_types.Part.from_text(text=prompt_text),
            ],
            config=gai_types.GenerateContentConfig(
                max_output_tokens=32,
                response_mime_type="application/json",
                response_schema=bool,
            ),
        )
        return split_gemini_response(resp)
    import base64 as _base64
    from eXercise.ai_client import build_completion_kwargs
    # Force thinking off for this tiny yes/no call (32-token cap).
    _use_stream, kw = build_completion_kwargs(state.provider, 0, 32)
    b64 = _base64.b64encode(jpeg_bytes).decode()
    oa_prompt = prompt_text + '\n\nReturn JSON only with this shape: {"answer": <bool>}'
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": oa_prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]}]
    try:
        resp = state.oa.chat.completions.create(
            model=model_id, messages=msgs,
            response_format={"type": "json_object"}, **kw,
        )
    except Exception:  # noqa: BLE001
        resp = state.oa.chat.completions.create(model=model_id, messages=msgs, **kw)
    raw = resp.choices[0].message.content or ""
    thinking_text = getattr(resp.choices[0].message, "reasoning_content", "") or ""
    return raw, thinking_text


# ─────────── Public entry points ──────────────────────────────────────────────

def check_exam_blank_pages(
    exam_pdf: Path,
    artifact_dir: Path | None = None,
) -> tuple[BlankCheckStatus, str | None]:
    """Step 14: detect blank pages in the empty exam PDF (text-only LLM).

    Writes ``14_exam_blank_detection/blank_exam_pages.json`` to artifact_dir.
    Emits ``Checking N empty-exam pages …`` and a ``✓ Page i/N · {blank|content}``
    line per page. Returns ``(BlankCheckStatus, message)``; never raises
    SystemExit (the dispatcher owns warn-vs-SystemExit policy).
    """
    from eXercise.ai_client import parse_model_spec
    from xscore.shared.exam_paths import artifact_exam_blank_json_path
    from xscore.shared.terminal_ui import info_line, ok_line

    model_id, thinking, max_tok = parse_model_spec(
        os.environ.get("EXAM_BLANK_DETECTION_MODEL", "qwen3.6-flash")
    )

    client_or_err = _build_client_state(model_id)
    if isinstance(client_or_err, str):
        return BlankCheckStatus.INCONCLUSIVE, client_or_err
    state = client_or_err

    exam_texts = _exam_page_texts(exam_pdf)
    n_pages = len(exam_texts)
    info_line(f"Checking {n_pages} empty-exam pages for blanks …")
    blank_exam_pages = find_blank_exam_pages(
        state, exam_texts, model_id, artifact_dir,
        thinking_tokens=thinking, max_tokens=max_tok,
    )
    if blank_exam_pages is None:
        return (
            BlankCheckStatus.INCONCLUSIVE,
            "could not determine which exam pages are blank "
            "(model call failed or returned malformed response)",
        )

    width = len(str(n_pages))
    for i in range(1, n_pages + 1):
        label = "blank" if i in blank_exam_pages else "content"
        ok_line(f"Page {i:>{width}d}/{n_pages}  ·  {label}")

    result_doc = {"blank_exam_pages": sorted(blank_exam_pages)}
    if artifact_dir:
        bp_path = artifact_exam_blank_json_path(artifact_dir)
        bp_path.parent.mkdir(parents=True, exist_ok=True)
        bp_path.write_text(json.dumps(result_doc, indent=2), encoding="utf-8")

    if not blank_exam_pages:
        return BlankCheckStatus.PASSED, "no blank pages found in empty exam"
    n = len(blank_exam_pages)
    pages_label = (
        f"exam page{'s' if n != 1 else ''} {sorted(blank_exam_pages)} "
        f"{'are' if n != 1 else 'is'} blank"
    )
    return BlankCheckStatus.PASSED, pages_label


def check_student_handwriting(
    scan_pdf: Path,
    page_assignments: list["PageAssignment"],
    artifact_dir: Path | None = None,
    empty_exam_has_cover: bool | None = None,
    *,
    wide: bool = True,
) -> tuple[BlankCheckStatus, str | None]:
    """Step 15: check student scan pages for handwriting (vision LLM, parallel).

    Reads the step-14 artifact for the empty-exam blank set, then runs vision
    handwriting checks per (student × page). With ``wide=True`` (default,
    controlled by ``HANDWRITING_CHECK_WIDE`` in the dispatcher) every answer
    page is checked; with ``wide=False`` only step-14 blank pages are checked.

    Cover scan pages (``p_label == 1`` when the student has a cover) are
    always excluded from checks.

    Writes ``15_student_handwriting/handwriting.json`` with three per-student
    fields: ``blank_scan_pages`` (entries for blank-in-empty pages — drives
    the attach-to-previous extras logic in the marking page register),
    ``pages_without_handwriting`` (authoritative skip list for the marker;
    every scan page where the AI returned False), and ``inconclusive_pages``
    (model errors / malformed responses).

    Emits per-task progress lines ``✓ Page X/N: 'Student' · {has,no} handwriting · dur``
    via terminal_ui. Returns ``(BlankCheckStatus, message)``; never raises
    SystemExit (the dispatcher owns warn-vs-SystemExit policy).
    """
    from eXercise.ai_client import parse_model_spec
    from xscore.shared.exam_paths import (
        artifact_exam_blank_json_path,
        artifact_handwriting_dir,
        artifact_handwriting_json_path,
        artifact_handwriting_prompt_path,
    )
    from xscore.shared.terminal_ui import (
        format_duration,
        info_line,
        ok_line,
        warn_line,
    )

    if artifact_dir is None:
        return BlankCheckStatus.INCONCLUSIVE, "no artifact_dir provided"

    # ── Read step 14 artifact ────────────────────────────────────────────────
    exam_blank_path = artifact_exam_blank_json_path(artifact_dir)
    if not exam_blank_path.exists():
        return (
            BlankCheckStatus.INCONCLUSIVE,
            "step 14 artifact not found (14_exam_blank_detection/blank_exam_pages.json); "
            "run exam_blank_detection first",
        )
    try:
        blank_data = json.loads(exam_blank_path.read_text(encoding="utf-8"))
        blank_exam_pages: set[int] = set(blank_data.get("blank_exam_pages", []))
    except Exception as exc:  # noqa: BLE001
        return BlankCheckStatus.INCONCLUSIVE, f"could not read step 14 artifact: {exc}"

    # Narrow mode + zero blanks → nothing to do; write empty artifact and exit.
    # Wide mode keeps going regardless of step 14's blank set.
    if not blank_exam_pages and not wide:
        students_out = [
            {
                "student_name": a.student_name,
                "blank_scan_pages": [],
                "pages_without_handwriting": [],
                "inconclusive_pages": [],
            }
            for a in page_assignments
        ]
        artifact = {"students": students_out}
        hw_path = artifact_handwriting_json_path(artifact_dir)
        hw_path.parent.mkdir(parents=True, exist_ok=True)
        hw_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
        return BlankCheckStatus.PASSED, "no blank exam pages — no handwriting checks needed"

    model_id, thinking, max_tok = parse_model_spec(
        os.environ.get("HANDWRITING_CHECK_MODEL", "qwen3-vl-flash")
    )

    client_or_err = _build_client_state(model_id)
    if isinstance(client_or_err, str):
        return BlankCheckStatus.INCONCLUSIVE, client_or_err
    state = client_or_err

    cover_page_mode = any(a.cover_page_number is not None for a in page_assignments)
    cover_offset = 1 if (cover_page_mode and not empty_exam_has_cover) else 0

    # ── Build task list ──────────────────────────────────────────────────────
    # Two cover concepts at play:
    #   - student cover (a.cover_page_number is not None) → skip p_label == 1
    #   - empty-exam cover (cover_offset) → drives exam_page = p_label - cover_offset
    tasks: list[tuple[Any, int, int]] = []  # (assignment, exam_page, scan_page)
    if wide:
        for a in page_assignments:
            has_cover = a.cover_page_number is not None
            for p_label, scan_page in enumerate(a.page_numbers, 1):
                if has_cover and p_label == 1:
                    continue                # student cover scan page
                exam_page = p_label - cover_offset
                if exam_page < 1:
                    continue
                tasks.append((a, exam_page, scan_page))
    else:
        for a in page_assignments:
            for exam_page in sorted(blank_exam_pages):
                p_label = exam_page + cover_offset
                if p_label > len(a.page_numbers):
                    continue
                scan_page = a.page_numbers[p_label - 1]
                tasks.append((a, exam_page, scan_page))

    if not tasks:
        students_out = [
            {
                "student_name": a.student_name,
                "blank_scan_pages": [],
                "pages_without_handwriting": [],
                "inconclusive_pages": [],
            }
            for a in page_assignments
        ]
        artifact = {"students": students_out}
        hw_path = artifact_handwriting_json_path(artifact_dir)
        hw_path.parent.mkdir(parents=True, exist_ok=True)
        hw_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
        if wide:
            msg = "no answer pages to check (every student's scan range is empty)"
        else:
            msg = (
                f"exam pages {sorted(blank_exam_pages)} are beyond every student's scan range — "
                "no handwriting checks needed"
            )
        return BlankCheckStatus.PASSED, msg

    jpeg_dir = artifact_handwriting_dir(artifact_dir)
    jpeg_dir.mkdir(parents=True, exist_ok=True)

    # Read scan PDF page count once for progress-line width formatting.
    import fitz
    with fitz.open(str(scan_pdf)) as _doc:
        scan_n_pages = _doc.page_count
    page_width = max(1, len(str(scan_n_pages)))
    name_width = max(
        (len(a.student_name or "Unknown") for a in page_assignments),
        default=1,
    )

    info_line(
        f"Checking {len(tasks)} (student × page) handwriting tasks "
        f"on scanned exam pages …"
    )

    # Sort tasks by scan_page so the reorder buffer below produces lines in
    # ascending page order (which also groups each student's pages together,
    # since page_numbers are contiguous per student).
    tasks_sorted = sorted(tasks, key=lambda t: t[2])

    def _detect(idx: int, args: tuple) -> tuple[int, str, int, int, bool | None, str]:
        """Returns (idx, student_name, exam_page, scan_page, has_handwriting_or_None, dur_str)."""
        assignment, exam_page, scan_page = args
        safe_name = (assignment.student_name or "Unknown").replace(" ", "_")
        jpeg_bytes = _render_page_jpeg(scan_pdf, scan_page)
        (jpeg_dir / f"{safe_name}_{exam_page}.jpg").write_bytes(jpeg_bytes)
        save_path = artifact_handwriting_prompt_path(
            artifact_dir, f"blank_{safe_name}_{exam_page}"
        )
        t0 = time.perf_counter()
        hw = _has_handwriting(state, model_id, jpeg_bytes, save_path)
        dur = format_duration(time.perf_counter() - t0)
        return idx, assignment.student_name, exam_page, scan_page, hw, dur

    def _emit(student_name: str, scan_page: int, hw: bool | None, dur: str) -> None:
        name_quoted = f"{student_name!r}"
        # +2 to padding for the surrounding quotes added by repr().
        if hw is None:
            warn_line(
                f"Page {scan_page:>{page_width}d}/{scan_n_pages}: "
                f"{name_quoted:<{name_width + 2}}  ·  inconclusive    ·  {dur}"
            )
        else:
            label = "has handwriting" if hw else "no handwriting "
            ok_line(
                f"Page {scan_page:>{page_width}d}/{scan_n_pages}: "
                f"{name_quoted:<{name_width + 2}}  ·  {label}  ·  {dur}"
            )

    results: list[tuple[str, int, int, bool | None]] = []
    pending: dict[int, tuple[str, int, int, bool | None, str]] = {}
    next_idx = 0
    workers = min(len(tasks_sorted), int(os.environ.get("HANDWRITING_WORKERS", "500")))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_detect, i, t): i for i, t in enumerate(tasks_sorted)}
        for fut in as_completed(futs):
            idx, student_name, exam_page, scan_page, hw, dur = fut.result()
            pending[idx] = (student_name, exam_page, scan_page, hw, dur)
            # Drain consecutive ready entries so output stays in submission
            # order while remaining incremental as workers complete.
            while next_idx in pending:
                sn, ep, sp, h, d = pending.pop(next_idx)
                _emit(sn, sp, h, d)
                results.append((sn, ep, sp, h))
                next_idx += 1

    # ── Build per-student artifact ───────────────────────────────────────────
    if blank_exam_pages:
        non_blank = set(range(1, max(blank_exam_pages) + 1)) - blank_exam_pages
    else:
        non_blank = set()

    def _attach_target(exam_page: int) -> int | None:
        candidates = [p for p in non_blank if p < exam_page]
        return max(candidates) if candidates else None

    by_student: dict[str, list[tuple[int, int, bool | None]]] = {}
    for student_name, exam_page, scan_page, has_hw in results:
        by_student.setdefault(student_name, []).append((exam_page, scan_page, has_hw))

    students_out = []
    inconclusive_total: list[tuple[str, int, int]] = []
    for a in page_assignments:
        entries = sorted(by_student.get(a.student_name, []), key=lambda x: x[0])
        blank_scan_pages: list[dict] = []
        pages_without_hw: list[int] = []
        inconclusive_pages: list[dict] = []
        for exam_page, scan_page, has_hw in entries:
            if has_hw is None:
                inconclusive_pages.append({
                    "exam_page": exam_page,
                    "scan_page": scan_page,
                    "reason": "handwriting check failed (model error or malformed response)",
                })
                inconclusive_total.append((a.student_name, exam_page, scan_page))
                continue
            if not has_hw:
                pages_without_hw.append(scan_page)
            # blank_scan_pages keeps its today-semantics: only blank-in-empty
            # entries, used by the register's attach-to-previous extras logic.
            if exam_page in blank_exam_pages:
                attach_exam = _attach_target(exam_page) if has_hw else None
                attach_scan_page: int | None = None
                if attach_exam is not None:
                    attach_p_label = attach_exam + cover_offset
                    if 1 <= attach_p_label <= len(a.page_numbers):
                        attach_scan_page = a.page_numbers[attach_p_label - 1]
                blank_scan_pages.append({
                    "exam_page": exam_page,
                    "scan_page": scan_page,
                    "has_handwriting": has_hw,
                    "attach_to_exam_page": attach_exam,
                    "attach_to_scan_page": attach_scan_page,
                })
        students_out.append({
            "student_name": a.student_name,
            "blank_scan_pages": blank_scan_pages,
            "pages_without_handwriting": sorted(pages_without_hw),
            "inconclusive_pages": inconclusive_pages,
        })

    artifact = {"students": students_out}
    hw_path = artifact_handwriting_json_path(artifact_dir)
    hw_path.parent.mkdir(parents=True, exist_ok=True)
    hw_path.write_text(
        json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    hw_count = sum(1 for _, _, hw in [e for s in by_student.values() for e in s] if hw is True)
    n_done = sum(1 for _, _, hw in [e for s in by_student.values() for e in s] if hw is not None)
    n_total = len(results)

    if blank_exam_pages:
        pages_label = (
            f"exam page{'s' if len(blank_exam_pages) != 1 else ''} {sorted(blank_exam_pages)}"
        )
    else:
        pages_label = "all answer pages"
    hw_label = "no handwriting" if hw_count == 0 else f"{hw_count}/{n_done} with handwriting"

    if inconclusive_total:
        names_pages = ", ".join(
            f"{name} page {sp}" for name, _ep, sp in inconclusive_total[:10]
        )
        more = (
            f" (and {len(inconclusive_total) - 10} more)"
            if len(inconclusive_total) > 10 else ""
        )
        msg = (
            f"Verified {n_done}/{n_total} (student × page) handwriting checks; "
            f"could not verify: {names_pages}{more} — these scan pages will not be "
            "attached to any answer; review manually if continuation work is suspected."
        )
        return BlankCheckStatus.INCONCLUSIVE, msg

    return BlankCheckStatus.PASSED, f"{pages_label} — {hw_label}"
