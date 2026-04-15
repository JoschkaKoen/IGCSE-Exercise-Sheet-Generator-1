"""Assign PDF pages to students by reading names from the top of each page.

Step E of the pipeline:
1. Render each page of the cleaned scan PDF.
2. Crop the top 15 % (name area only) and send to Kimi.
3. Fuzzy-match the returned name against the student roster.
4. Group consecutive pages: if page N+1 has no recognisable name, it belongs to
   the same student as page N.

Returns a list of ``PageAssignment`` objects.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from xscore.config import NAME_RECOGNITION_DPI

from .kimi_helpers import KimiChatClient, kimi_image_call, page_to_jpeg_b64, parse_json_safe
from xscore.shared.models import PageAssignment


_NAME_PROMPT = """\
Look at the top of this exam page. What is the student's name written here?

Return ONLY a JSON object:
{"name": "FirstName LastName"}

If no name is visible or the field is blank, return:
{"name": ""}
"""


def _crop_top(page, fraction: float = 0.15):
    """Return the top *fraction* of a PIL image."""
    w, h = page.size
    return page.crop((0, 0, w, int(h * fraction)))


def assign_pages(
    cleaned_pdf: Path,
    students: list[str],
    dpi: int = NAME_RECOGNITION_DPI,
    client: KimiChatClient | None = None,
    name_crop_fraction: float = 0.15,
    *,
    pages: list | None = None,
) -> list[PageAssignment]:
    """Return a ``PageAssignment`` for every student whose pages were found.

    If *client* is None it is created via ``KimiProvider.create_client()``.
    Logs sparse progress (not every page).
    *pages*: optional pre-rendered page images at *dpi* (skips ``convert_from_path``).
    """
    from xscore.extraction.ground_truth import fuzzy_match_name
    from pdf2image import convert_from_path

    if client is None:
        from xscore.extraction.providers.kimi import KimiProvider
        client = KimiProvider.create_client()
    if client is None:
        raise RuntimeError("No Kimi client available for page assignment.")

    from xscore.shared.terminal_ui import info_line, tool_line

    if pages is None:
        tool_line("pages", f"Rendering pages @ {dpi} DPI …")
        pages = convert_from_path(str(cleaned_pdf), dpi=dpi, thread_count=os.cpu_count() or 4)
    n_pages = len(pages)

    # Parallel OCR + fuzzy-match: each worker crops the page, calls Kimi, and
    # immediately fuzzy-matches the result against the roster.
    workers = int(os.environ.get("NAME_WORKERS", str(min(n_pages, 8))))

    def _ocr_and_match(args: tuple[int, Any]) -> tuple[int, str | None]:
        i, page = args
        crop = _crop_top(page, fraction=name_crop_fraction)
        img_b64 = page_to_jpeg_b64(crop)
        raw = kimi_image_call(client, img_b64, _NAME_PROMPT, max_tokens=64)
        data = parse_json_safe(raw)
        raw_name = str(data.get("name", "") or "").strip()
        matched_name = fuzzy_match_name(raw_name, students) if raw_name else None
        info_line(f"Page {i:3d}/{n_pages}: {raw_name!r}  →  {matched_name!r}")
        return i, matched_name

    page_results: dict[int, str | None] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(_ocr_and_match, (i, page)): i
            for i, page in enumerate(pages, 1)
        }
        for fut in as_completed(futures):
            i, matched_name = fut.result()
            page_results[i] = matched_name

    # Restore page order for the grouping step below.
    matched: list[str | None] = [page_results[i] for i in range(1, n_pages + 1)]

    # Group consecutive pages: a None name inherits from the previous match
    assignments: dict[str, list[int]] = {}
    current_student: str | None = None

    for page_num, name in enumerate(matched, 1):
        if name is not None:
            current_student = name
        if current_student is None:
            continue
        assignments.setdefault(current_student, []).append(page_num)

    result = [
        PageAssignment(
            student_name=name,
            page_numbers=pages_list,
            confidence="high" if matched[pages_list[0] - 1] is not None else "low",
        )
        for name, pages_list in assignments.items()
    ]
    return result


def page_assignments_to_json(assignments: list[PageAssignment]) -> str:
    """Serialise a PageAssignment list to a JSON string."""
    import json

    return json.dumps(
        [
            {
                "student_name": a.student_name,
                "page_numbers": a.page_numbers,
                "confidence": a.confidence,
            }
            for a in assignments
        ],
        indent=2,
        ensure_ascii=False,
    )


def page_assignments_to_md(assignments: list[PageAssignment]) -> str:
    """Return a markdown table of student → scan pages."""
    lines = [
        "# Exam Student List (scan-detected)\n",
        "| # | Student | Pages | Confidence |",
        "|---|---------|-------|------------|",
    ]
    for i, a in enumerate(assignments, 1):
        pages = ", ".join(str(p) for p in a.page_numbers)
        lines.append(f"| {i} | {a.student_name} | {pages} | {a.confidence} |")
    return "\n".join(lines) + "\n"
