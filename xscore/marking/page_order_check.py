"""Step 12 sub-step: verify that scan pages are in the correct order and content."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from xscore.shared.models import PageAssignment


class _PageIssue(TypedDict):
    position: int
    scan_page: int
    expected: str
    found: str
    detail: str


class _StudentResult(TypedDict):
    name: str
    ok: bool
    issues: list[_PageIssue]


class _PageOrderResult(TypedDict):
    all_ok: bool
    students: list[_StudentResult]


def _exam_page_texts(exam_pdf: Path) -> list[str]:
    import fitz
    with fitz.open(str(exam_pdf)) as doc:
        return [doc[i].get_text().strip() for i in range(doc.page_count)]


def _scan_page_texts(
    scan_pdf: Path,
    page_nums: list[int],  # 1-based
    dpi: int = 150,
) -> list[str]:
    import fitz
    from concurrent.futures import ThreadPoolExecutor
    from xscore.preprocessing.assign_pages_to_students import _get_ocr

    def _ocr_one(p: int) -> str:
        with fitz.open(str(scan_pdf)) as doc:
            pix = doc[p - 1].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        ocr_out, _ = _get_ocr()(pix.tobytes("png"))
        return "\n".join(t for _, t, c in (ocr_out or []) if float(c) > 0.8)

    with ThreadPoolExecutor(max_workers=min(len(page_nums), 8)) as ex:
        return list(ex.map(_ocr_one, page_nums))


def _format_text_artifact(sections: list[tuple[str, str]]) -> str:
    parts = []
    for label, text in sections:
        parts.append(f"=== {label} ===\n{text or '(no text)'}")
    return "\n\n".join(parts)


def _build_prompt(exam_texts: list[str], students_data: list[dict]) -> str:
    lines = [
        "You are verifying that each student's scanned exam pages are in the correct order",
        "and contain the correct content.",
        "",
        f"EMPTY EXAM PAGES (exact printed text, {len(exam_texts)} pages):",
    ]
    for i, text in enumerate(exam_texts, 1):
        lines += [f"Page {i}:", text or "(no printed text)", ""]

    lines += ["", "STUDENT SCANS (OCR of printed text only, handwriting excluded):"]
    for s in students_data:
        lines.append(f"Student: {s['name']}")
        for pos, (scan_p, text) in enumerate(zip(s["scan_pages"], s["texts"]), 1):
            lines += [f"  Position {pos} (scan page {scan_p}):", f"  {text or '(no text)'}", ""]

    lines += [
        "Your task: detect pages that are physically out of order in the student's scan.",
        "A mismatch means the SEQUENCE of questions is wrong — e.g. the page at position 5 contains",
        "question 8's text when it should contain question 5's text.",
        "To detect this: identify the question number(s) and question text visible on each page,",
        "then check that the sequence in the student scan matches the sequence in the empty exam.",
        "Both the reference text (PDF heuristic extraction) and the scan text (OCR) are imperfect —",
        "focus on the identity and order of questions, not exact wording / spelling.",
        "Ignore all student handwriting, answer variations, and minor OCR noise.",
        "Only flag when a question clearly belongs to a different position in the exam.",
    ]
    return "\n".join(lines)


def check_page_order(
    exam_pdf: Path,
    scan_pdf: Path,
    page_assignments: list["PageAssignment"],
    artifact_dir: Path | None = None,
) -> None:
    """Validate page order and content for all students. Raises SystemExit(1) on mismatch."""
    import os
    from xscore.shared.terminal_ui import info_line, ok_line, warn_line
    from eXercise.ai_client import (
        build_gemini_thinking_config,
        make_gemini_native_client,
        parse_model_spec,
    )
    from xscore.shared.prompt_logger import save_prompt, save_response
    from xscore.shared.exam_paths import (
        artifact_page_order_empty_exam_txt_path,
        artifact_page_order_prompt_path,
        artifact_page_order_txt_path,
    )
    from google.genai import types as gai_types
    _gai = make_gemini_native_client()
    if _gai is None:
        warn_line("GEMINI_API_KEY not set — page order check skipped")
        return
    model_id, _thinking, _max_tok = parse_model_spec(
        os.environ.get("PAGE_ORDER_CHECK_MODEL", "gemini-2.5-flash-lite")
    )

    # ── Extract text ──────────────────────────────────────────────────────────
    exam_texts = _exam_page_texts(exam_pdf)

    if artifact_dir:
        _po_empty = artifact_page_order_empty_exam_txt_path(artifact_dir)
        _po_empty.parent.mkdir(parents=True, exist_ok=True)
        _po_empty.write_text(
            _format_text_artifact([(f"Page {i}", t) for i, t in enumerate(exam_texts, 1)]),
            encoding="utf-8",
        )

    # Collect all scan pages from all students at once, OCR in one parallel batch.
    all_page_nums: list[int] = []
    for a in page_assignments:
        all_page_nums.extend(a.page_numbers)
    all_texts = _scan_page_texts(scan_pdf, all_page_nums)
    page_text_map: dict[int, str] = dict(zip(all_page_nums, all_texts))

    students_data = []
    for a in page_assignments:
        texts = [page_text_map[p] for p in a.page_numbers]
        students_data.append({"name": a.student_name, "scan_pages": a.page_numbers, "texts": texts})

        if artifact_dir:
            _po_student = artifact_page_order_txt_path(artifact_dir, a.student_name)
            _po_student.parent.mkdir(parents=True, exist_ok=True)
            _po_student.write_text(
                _format_text_artifact([
                    (f"Position {pos} (scan page {sp})", t)
                    for pos, (sp, t) in enumerate(zip(a.page_numbers, texts), 1)
                ]),
                encoding="utf-8",
            )

    # ── Call model ────────────────────────────────────────────────────────────
    prompt = _build_prompt(exam_texts, students_data)
    save_path = artifact_page_order_prompt_path(artifact_dir) if artifact_dir else None
    save_prompt(save_path, model=model_id, messages=[{"role": "user", "content": prompt}])

    import time as _time
    t0 = _time.perf_counter()
    _cfg_kwargs: dict = {
        "max_output_tokens": _max_tok or 2048,
        "response_mime_type": "application/json",
        "response_schema": _PageOrderResult,
    }
    if _thinking is not None:
        _cfg_kwargs["thinking_config"] = build_gemini_thinking_config(_thinking)
    resp = _gai.models.generate_content(
        model=model_id,
        contents=[gai_types.Part.from_text(text=prompt)],
        config=gai_types.GenerateContentConfig(**_cfg_kwargs),
    )
    dur = round(_time.perf_counter() - t0, 1)
    raw = resp.text or ""
    save_response(save_path, raw)

    # ── Parse and report ──────────────────────────────────────────────────────
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    if data.get("all_ok", True):
        ok_line(f"Page order check: all students OK  ·  {dur}s")
        return

    error_lines = ["Scan page order / content mismatch:", ""]
    for s in data.get("students", []):
        if s.get("ok", True) or not s.get("issues"):
            continue
        error_lines.append(f"  {s['name']}:")
        for issue in s["issues"]:
            error_lines.append(
                f"    Position {issue.get('position')} (scan page {issue.get('scan_page')}): "
                f"{issue.get('detail', '')}  —  "
                f"expected: {issue.get('expected', '?')} / found: {issue.get('found', '?')}"
            )
    error_lines += ["", "  Re-scan the affected booklet(s) in the correct page order and re-run."]
    warn_line("\n".join(error_lines))
    raise SystemExit(1)
