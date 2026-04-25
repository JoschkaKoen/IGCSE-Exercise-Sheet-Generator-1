"""Step 13 sub-step: detect blank pages in the empty exam and check for student handwriting."""

from __future__ import annotations

import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xscore.shared.models import PageAssignment


def _exam_page_texts(exam_pdf: Path) -> list[str]:
    import fitz
    with fitz.open(str(exam_pdf)) as doc:
        return [doc[i].get_text().strip() for i in range(doc.page_count)]


def _render_page_jpeg(pdf_path: Path, page_1based: int, dpi: int = 150) -> bytes:
    import fitz
    with fitz.open(str(pdf_path)) as doc:
        pix = doc[page_1based - 1].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    return pix.tobytes("jpeg")


def _parse_blank_pages(raw: str) -> set[int]:
    """Parse blank-page list from either Gemini ``[1,2,3]`` or OA ``{"blank_pages":[...]}``."""
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return set()
    if isinstance(data, list):
        pages = data
    elif isinstance(data, dict):
        pages = data.get("blank_pages") or data.get("pages") or []
    else:
        return set()
    try:
        return {int(p) for p in pages}
    except (TypeError, ValueError):
        return set()


def find_blank_exam_pages(
    exam_texts: list[str],
    gai_client,
    model_id: str,
    artifact_dir: Path | None,
    *,
    thinking_tokens: int | None = None,
    max_tokens: int | None = None,
) -> set[int]:
    """One LLM text call to identify blank exam pages. Returns set of 1-based page numbers.

    Routes to the right provider based on *model_id*; *gai_client* is used only
    on the Gemini branch.
    """
    from xscore.shared.prompt_logger import save_prompt, save_response
    from xscore.shared.exam_paths import artifact_blank_detection_txt_path, artifact_blank_pages_prompt_path

    lines = [
        "You are analysing an empty exam paper.",
        "Below is the printed text from each page.",
        "",
    ]
    for i, text in enumerate(exam_texts, 1):
        lines += [f"Page {i}:", text or "(no printed text)", ""]

    lines += [
        "Identify all BLANK pages. A blank page:",
        "- Contains the words \"BLANK PAGE\"",
        "- Has NO exercise instructions or question text",
        "- May have printed horizontal lines (writing lines for students) — these do NOT",
        "  disqualify a page from being blank",
        "",
        "Return the list of 1-based page numbers of blank pages.",
    ]
    prompt = "\n".join(lines)

    if artifact_dir:
        _det_path = artifact_blank_detection_txt_path(artifact_dir)
        _det_path.parent.mkdir(parents=True, exist_ok=True)
        _det_path.write_text(prompt, encoding="utf-8")

    save_path = artifact_blank_pages_prompt_path(artifact_dir, "blank_detection_exam") if artifact_dir else None
    save_prompt(save_path, model=model_id, messages=[{"role": "user", "content": prompt}])

    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        from eXercise.ai_client import build_gemini_thinking_config
        _cfg_kwargs: dict = {
            "max_output_tokens": max_tokens or 256,
            "response_mime_type": "application/json",
            "response_schema": list[int],
        }
        if thinking_tokens is not None:
            _cfg_kwargs["thinking_config"] = build_gemini_thinking_config(thinking_tokens)
        resp = gai_client.models.generate_content(
            model=model_id,
            contents=[gai_types.Part.from_text(text=prompt)],
            config=gai_types.GenerateContentConfig(**_cfg_kwargs),
        )
        raw = resp.text or ""
    else:
        from xscore.shared.terminal_ui import warn_line
        from eXercise.ai_client import (
            build_completion_kwargs,
            collect_streamed_response,
            make_ai_client,
        )
        _result = make_ai_client(model_env="BLANK_PAGE_DETECTION_MODEL")
        if _result is None:
            warn_line(
                f"BLANK_PAGE_DETECTION_MODEL={model_id} requires API key — "
                f"no blank pages detected"
            )
            return set()
        _oa_client, _, _provider, _, _ = _result
        _use_stream, _kw = build_completion_kwargs(_provider, thinking_tokens, max_tokens or 256)
        _oa_prompt = prompt + (
            '\n\nReturn JSON only with this shape: {"blank_pages": [<int>, ...]}'
        )
        _msgs = [{"role": "user", "content": _oa_prompt}]
        if _use_stream:
            _stream = _oa_client.chat.completions.create(
                model=model_id, messages=_msgs, stream=True, **_kw,
            )
            raw = collect_streamed_response(_stream)
        else:
            try:
                _resp = _oa_client.chat.completions.create(
                    model=model_id, messages=_msgs,
                    response_format={"type": "json_object"}, **_kw,
                )
            except Exception:
                _resp = _oa_client.chat.completions.create(
                    model=model_id, messages=_msgs, **_kw,
                )
            raw = _resp.choices[0].message.content or ""

    save_response(save_path, raw)
    return _parse_blank_pages(raw)


def _has_handwriting(
    gai_client,
    model_id: str,
    jpeg_bytes: bytes,
    save_path: Path | None,
) -> bool:
    """Vision call: does this blank scan page contain student handwriting?

    Routes to the right provider based on *model_id*; *gai_client* is used only
    on the Gemini branch.
    """
    from xscore.shared.prompt_logger import save_prompt, save_response

    prompt_text = (
        "This is a blank exam page. It may have printed horizontal writing lines.\n"
        "Is there any STUDENT HANDWRITING on this page? "
        "Ignore the printed lines — only count ink written by a student.\n"
        "IMPORTANT: Some pages show faint marks that bleed through from ink written on the "
        "other side of the paper (show-through). Do NOT count these — only report handwriting "
        "that is clearly and deliberately written on THIS side of the page."
    )
    save_prompt(save_path, model=model_id, messages=[{"role": "user", "content": prompt_text}])

    if model_id.startswith("gemini"):
        from google.genai import types as gai_types
        resp = gai_client.models.generate_content(
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
        raw = resp.text or ""
    else:
        import base64 as _base64
        from xscore.shared.terminal_ui import warn_line
        from eXercise.ai_client import (
            build_completion_kwargs,
            collect_streamed_response,
            make_ai_client,
        )
        _result = make_ai_client(model_env="BLANK_PAGE_DETECTION_MODEL")
        if _result is None:
            warn_line(
                f"BLANK_PAGE_DETECTION_MODEL={model_id} requires API key — "
                f"assuming no handwriting"
            )
            return False
        _oa_client, _, _provider, _, _ = _result
        # Force thinking off for this tiny yes/no call (32-token cap).
        _use_stream, _kw = build_completion_kwargs(_provider, 0, 32)
        _b64 = _base64.b64encode(jpeg_bytes).decode()
        _oa_prompt = prompt_text + '\n\nReturn JSON only with this shape: {"answer": <bool>}'
        _msgs = [{"role": "user", "content": [
            {"type": "text", "text": _oa_prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_b64}"}},
        ]}]
        try:
            _resp = _oa_client.chat.completions.create(
                model=model_id, messages=_msgs,
                response_format={"type": "json_object"}, **_kw,
            )
        except Exception:
            _resp = _oa_client.chat.completions.create(
                model=model_id, messages=_msgs, **_kw,
            )
        raw = _resp.choices[0].message.content or ""

    save_response(save_path, raw)
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return False
    if isinstance(data, bool):
        return data
    if isinstance(data, dict):
        return bool(data.get("answer", data.get("has_handwriting", False)))
    return False


def check_blank_pages(
    exam_pdf: Path,
    scan_pdf: Path,
    page_assignments: list["PageAssignment"],
    artifact_dir: Path | None = None,
    empty_exam_has_cover: bool | None = None,
) -> None:
    """Detect blank pages in the empty exam, then check each student's blank scan pages
    for handwriting. Writes ``13_blank_pages.json`` to artifact_dir.

    *empty_exam_has_cover* — True when the empty exam's first page is a cover page.
    When True the scan's cover page (p_label=1) maps 1:1 to exam page 1, so no offset
    is needed.  When False/None (empty exam has no cover), the scan cover page shifts
    all answer pages by +1 relative to the empty exam page numbers.
    """
    from eXercise.ai_client import make_gemini_native_client, parse_model_spec
    from xscore.shared.exam_paths import (
        artifact_blank_detection_txt_path,
        artifact_blank_pages_dir,
        artifact_blank_pages_json_path,
        artifact_blank_pages_prompt_path,
    )
    from xscore.shared.terminal_ui import info_line, ok_line, warn_line

    model_id, _thinking, _max_tok = parse_model_spec(
        os.environ.get("BLANK_PAGE_DETECTION_MODEL", "gemini-2.5-flash-lite")
    )
    # Only the Gemini branch needs a Gemini client; helpers route internally.
    gai_client = None
    if model_id.startswith("gemini"):
        gai_client = make_gemini_native_client()
        if gai_client is None:
            warn_line("GEMINI_API_KEY not set — blank page detection skipped")
            return

    # ── 1. Find blank pages in the empty exam ────────────────────────────────
    import time as _time
    exam_texts = _exam_page_texts(exam_pdf)
    t0 = _time.perf_counter()
    blank_exam_pages = find_blank_exam_pages(
        exam_texts, gai_client, model_id, artifact_dir,
        thinking_tokens=_thinking, max_tokens=_max_tok,
    )
    detect_dur = round(_time.perf_counter() - t0, 1)

    empty_artifact = {"blank_exam_pages": [], "students": []}
    if not blank_exam_pages:
        ok_line(f"Blank page detection: no blank pages found in empty exam  ·  {detect_dur}s")
        if artifact_dir:
            _bp_path = artifact_blank_pages_json_path(artifact_dir)
            _bp_path.parent.mkdir(parents=True, exist_ok=True)
            _bp_path.write_text(json.dumps(empty_artifact, indent=2), encoding="utf-8")
        return

    _blank_pages_found = sorted(blank_exam_pages)

    cover_page_mode = any(a.cover_page_number is not None for a in page_assignments)
    # Offset is needed only when the scan has a cover page that the empty exam does not.
    # When the empty exam also starts with a cover page, both are aligned at position 1.
    cover_offset = 1 if (cover_page_mode and not empty_exam_has_cover) else 0

    # ── 2. For each student × blank page: render JPEG + detect handwriting ──
    # Build tasks: (assignment, exam_page, scan_page)
    tasks: list[tuple[object, int, int]] = []
    for a in page_assignments:
        for exam_page in sorted(blank_exam_pages):
            p_label = exam_page + cover_offset
            if p_label > len(a.page_numbers):
                continue
            scan_page = a.page_numbers[p_label - 1]
            tasks.append((a, exam_page, scan_page))

    if not tasks:
        ok_line("Blank page detection: all blank exam pages are beyond every student's scan range — skipping handwriting check")
        students_out = [{"student_name": a.student_name, "blank_scan_pages": []} for a in page_assignments]
        artifact = {"blank_exam_pages": sorted(blank_exam_pages), "students": students_out}
        if artifact_dir:
            _bp_path = artifact_blank_pages_json_path(artifact_dir)
            _bp_path.parent.mkdir(parents=True, exist_ok=True)
            _bp_path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
        return

    jpeg_dir = artifact_blank_pages_dir(artifact_dir) if artifact_dir else None
    if jpeg_dir:
        jpeg_dir.mkdir(parents=True, exist_ok=True)

    def _detect(args: tuple) -> tuple[str, int, int, bool]:
        """Returns (student_name, exam_page, scan_page, has_handwriting)."""
        assignment, exam_page, scan_page = args
        safe_name = (assignment.student_name or "Unknown").replace(" ", "_")
        jpeg_bytes = _render_page_jpeg(scan_pdf, scan_page)

        if jpeg_dir:
            (jpeg_dir / f"{safe_name}_{exam_page}.jpg").write_bytes(jpeg_bytes)

        save_path = (
            artifact_blank_pages_prompt_path(artifact_dir, f"blank_{safe_name}_{exam_page}")
            if artifact_dir else None
        )
        hw = _has_handwriting(gai_client, model_id, jpeg_bytes, save_path)
        return assignment.student_name, exam_page, scan_page, hw

    results: list[tuple[str, int, int, bool]] = []
    workers = min(len(tasks), 8)
    t_hw = _time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_detect, t): t for t in tasks}
        for fut in as_completed(futs):
            results.append(fut.result())
    hw_dur = round(_time.perf_counter() - t_hw, 1)

    # ── 3. Compute attach_to_exam_page + build artifact ──────────────────────
    non_blank = set(range(1, len(exam_texts) + 1)) - blank_exam_pages

    def _attach_target(exam_page: int) -> int | None:
        candidates = [p for p in non_blank if p < exam_page]
        return max(candidates) if candidates else None

    # Group results by student
    by_student: dict[str, list[tuple[int, int, bool]]] = {}
    for student_name, exam_page, scan_page, has_hw in results:
        by_student.setdefault(student_name, []).append((exam_page, scan_page, has_hw))

    students_out = []
    for a in page_assignments:
        student_entries = sorted(by_student.get(a.student_name, []), key=lambda x: x[0])
        blank_scan_pages = []
        for exam_page, scan_page, has_hw in student_entries:
            attach_exam = _attach_target(exam_page) if has_hw else None
            attach_scan_page: int | None = None
            if attach_exam is not None:
                attach_p_label = attach_exam + cover_offset
                if 1 <= attach_p_label <= len(a.page_numbers):
                    attach_scan_page = a.page_numbers[attach_p_label - 1]
            entry: dict = {
                "exam_page": exam_page,
                "scan_page": scan_page,
                "has_handwriting": has_hw,
                "attach_to_exam_page": attach_exam,
                "attach_to_scan_page": attach_scan_page,
            }
            blank_scan_pages.append(entry)
        students_out.append({"student_name": a.student_name, "blank_scan_pages": blank_scan_pages})

    artifact = {
        "blank_exam_pages": sorted(blank_exam_pages),
        "students": students_out,
    }
    if artifact_dir:
        _bp_path = artifact_blank_pages_json_path(artifact_dir)
        _bp_path.parent.mkdir(parents=True, exist_ok=True)
        _bp_path.write_text(
            json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    hw_count = sum(1 for _, _, hw in [e for s in by_student.values() for e in s] if hw)
    total_dur = round(_time.perf_counter() - t0, 1)
    _n_blank = len(_blank_pages_found)
    _pages_label = f"exam page{'s' if _n_blank != 1 else ''} {_blank_pages_found}"
    _hw_label = "no handwriting" if hw_count == 0 else f"{hw_count}/{len(results)} with handwriting"
    ok_line(f"Blank page detection: {_pages_label} — {_hw_label}  ·  {total_dur}s")
