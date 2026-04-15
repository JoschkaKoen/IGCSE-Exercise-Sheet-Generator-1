"""Assign PDF pages to students by reading names from the top of each page.

Step 10 sub-step of the pipeline:
1. Render each page of the cleaned scan PDF at *dpi*.
2. Crop the top fraction (name area) and send to the vision model.
3. Fuzzy-match the returned name against the student roster.
4. Group pages into fixed blocks of *pages_per_student* (from geometry).
   Undetected names become ``Unknown_N`` entries rather than inheriting
   from a neighbouring student.

Model is resolved from ``NAME_DETECTION_MODEL`` env var (default ``kimi-k2.5``).
Worker count is resolved from ``NAME_WORKERS`` env var (default ``min(n_pages, 8)``).

Returns a list of ``PageAssignment`` objects (one per student block).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from xscore.config import NAME_RECOGNITION_DPI

from .kimi_helpers import kimi_image_call, page_to_jpeg_b64, parse_json_safe
from xscore.shared.models import PageAssignment


_NAME_PROMPT = """\
Look at the top of this exam page for the student's HANDWRITTEN name.

Ignore all pre-printed or typed text: exam codes, stamps, watermarks, \
school names, or labels (e.g. "EMPL", "EMPI", page numbers).

Return ONLY a JSON object:
{"name": "FirstName LastName"}

If no handwritten name is visible or the name field is blank, return:
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
    pages_per_student: int = 1,
    name_crop_fraction: float = 0.15,
    *,
    pages: list | None = None,
) -> list[PageAssignment]:
    """Return one ``PageAssignment`` per student block.

    Pages are grouped into fixed blocks of *pages_per_student* (as determined
    by exam geometry). The name is read from the first page of each block.
    Blocks with no detectable name are recorded as ``Unknown_N`` with
    ``confidence="low"`` instead of being merged into a neighbouring student.

    The vision model is configured via ``NAME_DETECTION_MODEL`` (default
    ``kimi-k2.5``). Worker parallelism via ``NAME_WORKERS`` (default
    ``min(n_pages, 8)``).

    *pages*: optional pre-rendered PIL images at *dpi* (skips PDF rendering).
    """
    from xscore.extraction.ground_truth import fuzzy_match_name
    from pdf2image import convert_from_path
    from eXercise.ai_client import make_ai_client

    ai_result = make_ai_client(model_env="NAME_DETECTION_MODEL", default_model="gemini-2.5-flash")
    if ai_result is None:
        raise RuntimeError(
            "NAME_DETECTION_MODEL client could not be created — check API key in .env"
        )
    client, model_id, _provider, _effort = ai_result

    from xscore.shared.terminal_ui import info_line, tool_line

    if pages is None:
        tool_line("pages", f"Rendering pages @ {dpi} DPI …")
        pages = convert_from_path(str(cleaned_pdf), dpi=dpi, thread_count=os.cpu_count() or 4)
    n_pages = len(pages)

    # Parallel OCR + fuzzy-match: each worker crops one page, calls the vision
    # model, and immediately fuzzy-matches the result against the roster.
    workers = int(os.environ.get("NAME_WORKERS", str(min(n_pages, 8))))

    def _ocr_and_match(args: tuple[int, Any]) -> tuple[int, str | None]:
        i, page = args
        crop = _crop_top(page, fraction=name_crop_fraction)
        img_b64 = page_to_jpeg_b64(crop)
        raw = kimi_image_call(client, img_b64, _NAME_PROMPT, max_tokens=64, model_id=model_id)
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

    # Restore page order for the block-grouping step below.
    matched: list[str | None] = [page_results[i] for i in range(1, n_pages + 1)]
    del pages  # free PIL image list

    # Group into fixed blocks of pages_per_student (guaranteed by geometry).
    # Undetected first pages become Unknown_N — no cross-student inheritance.
    n_blocks = n_pages // pages_per_student
    result: list[PageAssignment] = []
    for b in range(n_blocks):
        first_idx = b * pages_per_student           # 0-based index of block's first page
        name = matched[first_idx]
        if name is None:
            name = f"Unknown_{b + 1}"
            confidence = "low"
        else:
            confidence = "high"
        block_pages = list(range(first_idx + 1, first_idx + pages_per_student + 1))  # 1-based
        result.append(PageAssignment(
            student_name=name,
            page_numbers=block_pages,
            confidence=confidence,
        ))
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
