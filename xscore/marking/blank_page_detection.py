"""Step 8 sub-step: detect blank pages in the empty exam and check for student handwriting."""

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


def find_blank_exam_pages(
    exam_texts: list[str],
    gai_client,
    model_id: str,
    artifact_dir: Path | None,
) -> set[int]:
    """One LLM text call to identify blank exam pages. Returns set of 1-based page numbers."""
    from google.genai import types as gai_types
    from xscore.shared.prompt_logger import save_prompt, save_response
    from xscore.shared.exam_paths import artifact_prompt_path

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
        (artifact_dir / "8_blank_detection_empty_exam.txt").write_text(
            prompt, encoding="utf-8"
        )

    save_path = artifact_prompt_path(artifact_dir, "8_blank_detection_exam") if artifact_dir else None
    save_prompt(save_path, model=model_id, messages=[{"role": "user", "content": prompt}])

    resp = gai_client.models.generate_content(
        model=model_id,
        contents=[gai_types.Part.from_text(text=prompt)],
        config=gai_types.GenerateContentConfig(
            max_output_tokens=256,
            response_mime_type="application/json",
            response_schema=list[int],
        ),
    )
    raw = resp.text or ""
    save_response(save_path, raw)

    try:
        pages = json.loads(raw) if raw else []
        return {int(p) for p in (pages if isinstance(pages, list) else [])}
    except (json.JSONDecodeError, ValueError):
        return set()


def _has_handwriting(
    gai_client,
    model_id: str,
    jpeg_bytes: bytes,
    save_path: Path | None,
) -> bool:
    """Vision call: does this blank scan page contain student handwriting?"""
    from google.genai import types as gai_types
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
    save_response(save_path, raw)

    try:
        return bool(json.loads(raw)) if raw else False
    except (json.JSONDecodeError, ValueError):
        return False


def check_blank_pages(
    exam_pdf: Path,
    scan_pdf: Path,
    page_assignments: list["PageAssignment"],
    artifact_dir: Path | None = None,
    empty_exam_has_cover: bool | None = None,
) -> None:
    """Detect blank pages in the empty exam, then check each student's blank scan pages
    for handwriting. Writes ``8_blank_pages.json`` to artifact_dir.

    *empty_exam_has_cover* — True when the empty exam's first page is a cover page.
    When True the scan's cover page (p_label=1) maps 1:1 to exam page 1, so no offset
    is needed.  When False/None (empty exam has no cover), the scan cover page shifts
    all answer pages by +1 relative to the empty exam page numbers.
    """
    from eXercise.ai_client import make_gemini_native_client, parse_model_effort
    from xscore.shared.exam_paths import artifact_prompt_path
    from xscore.shared.terminal_ui import info_line, ok_line, warn_line

    gai_client = make_gemini_native_client()
    if gai_client is None:
        warn_line("GEMINI_API_KEY not set — blank page detection skipped")
        return
    model_id, _effort = parse_model_effort(
        os.environ.get("BLANK_PAGE_DETECTION_MODEL", "gemini-2.5-flash-lite")
    )

    # ── 1. Find blank pages in the empty exam ────────────────────────────────
    import time as _time
    exam_texts = _exam_page_texts(exam_pdf)
    t0 = _time.perf_counter()
    blank_exam_pages = find_blank_exam_pages(exam_texts, gai_client, model_id, artifact_dir)
    detect_dur = round(_time.perf_counter() - t0, 1)

    empty_artifact = {"blank_exam_pages": [], "students": []}
    if not blank_exam_pages:
        ok_line(f"Blank page detection: no blank pages found in empty exam  ·  {detect_dur}s")
        if artifact_dir:
            (artifact_dir / "8_blank_pages.json").write_text(
                json.dumps(empty_artifact, indent=2), encoding="utf-8"
            )
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
            (artifact_dir / "8_blank_pages.json").write_text(
                json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        return

    jpeg_dir = artifact_dir / "8_blank_pages" if artifact_dir else None
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
            artifact_prompt_path(artifact_dir, f"8_blank_{safe_name}_{exam_page}")
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
        (artifact_dir / "8_blank_pages.json").write_text(
            json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    hw_count = sum(1 for _, _, hw in [e for s in by_student.values() for e in s] if hw)
    total_dur = round(_time.perf_counter() - t0, 1)
    _n_blank = len(_blank_pages_found)
    _pages_label = f"exam page{'s' if _n_blank != 1 else ''} {_blank_pages_found}"
    _hw_label = "no handwriting" if hw_count == 0 else f"{hw_count}/{len(results)} with handwriting"
    ok_line(f"Blank page detection: {_pages_label} — {_hw_label}  ·  {total_dur}s")
