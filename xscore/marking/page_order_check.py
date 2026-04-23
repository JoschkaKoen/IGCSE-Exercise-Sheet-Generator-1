"""Step 8 sub-step: verify that scan pages are in the correct order and content."""

from __future__ import annotations
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xscore.shared.models import PageAssignment


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
    from xscore.marking.assign_pages_to_students import _get_ocr

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
        "For each student check that position N matches empty exam page N in content.",
        "OCR may have minor noise — focus on question numbers, instructions, 'BLANK PAGE', section headings.",
        "",
        'Return ONLY JSON: {"all_ok": bool, "students": [{"name": str, "ok": bool, "issues": [{"position": int, "scan_page": int, "expected": str, "found": str, "detail": str}]}]}',
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
    from xscore.marking.ai_helpers import parse_json_safe
    from eXercise.ai_client import parse_model_effort
    from xscore.shared.prompt_logger import save_prompt, save_response
    from xscore.shared.exam_paths import artifact_prompt_path
    from google.genai import types as gai_types
    from google import genai as gai

    _api_key = (os.environ.get("GEMINI_API_KEY", "") or os.environ.get("GOOGLE_API_KEY", "")).strip()
    if not _api_key:
        warn_line("GEMINI_API_KEY not set — page order check skipped")
        return
    model_id, _effort = parse_model_effort(
        os.environ.get("PAGE_ORDER_CHECK_MODEL", "gemini-2.5-flash-lite")
    )
    _gai = gai.Client(api_key=_api_key)

    # ── Extract text ──────────────────────────────────────────────────────────
    exam_texts = _exam_page_texts(exam_pdf)

    if artifact_dir:
        (artifact_dir / "8_page_order_empty_exam.txt").write_text(
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
            safe_name = a.student_name.replace(" ", "_")
            (artifact_dir / f"8_page_order_student_{safe_name}.txt").write_text(
                _format_text_artifact([
                    (f"Position {pos} (scan page {sp})", t)
                    for pos, (sp, t) in enumerate(zip(a.page_numbers, texts), 1)
                ]),
                encoding="utf-8",
            )

    # ── Call model ────────────────────────────────────────────────────────────
    prompt = _build_prompt(exam_texts, students_data)
    save_path = artifact_prompt_path(artifact_dir, "8_page_order") if artifact_dir else None
    save_prompt(save_path, model=model_id, messages=[{"role": "user", "content": prompt}])

    import time as _time
    t0 = _time.perf_counter()
    resp = _gai.models.generate_content(
        model=model_id,
        contents=[gai_types.Part.from_text(text=prompt)],
        config=gai_types.GenerateContentConfig(max_output_tokens=2048),
    )
    dur = round(_time.perf_counter() - t0, 1)
    raw = resp.text or ""
    save_response(save_path, raw)

    # ── Parse and report ──────────────────────────────────────────────────────
    data = parse_json_safe(raw) or {}
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
