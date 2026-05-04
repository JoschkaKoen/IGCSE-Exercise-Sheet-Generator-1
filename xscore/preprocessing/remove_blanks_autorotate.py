"""Blank-page detection + page-preserving copy for class-scan PDFs.

``prepare_scans`` (step 4) is the single authority for orientation. It detects
per-scan-file rotation via AI vision or Tesseract OSD and bakes the result into
``merged_scan.pdf``'s ``/Rotate`` metadata. This module only renders pages once
at low DPI to classify blanks and then writes the kept pages through pikepdf,
preserving each page's existing ``/Rotate`` exactly as step 4 left it. No
rotation override happens here.

Used by :mod:`preprocessing.coordinator` before fine deskew. Formerly
``autograder.py``.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pikepdf
from pdf2image import convert_from_path
from PIL import Image

from xscore.config import BLANK_DETECTION_DPI as BLANK_DPI

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------
BLANK_MEAN_THRESHOLD = 250  # Pages with grayscale mean above this are considered blank (0-255)
BLANK_STD_THRESHOLD = 6     # Pages with grayscale std below this are considered blank


def is_blank_page(
    image: Image.Image,
    mean_threshold: float = BLANK_MEAN_THRESHOLD,
    std_threshold: float = BLANK_STD_THRESHOLD,
) -> bool:
    gray_img = image if image.mode == "L" else image.convert("L")
    gray = np.array(gray_img, dtype=np.float32)
    mean = gray.mean()
    std = gray.std()
    return (mean >= mean_threshold) and (std <= std_threshold)


def _raster_timed(label: str, fn) -> list:
    """Run *fn()* and print a dim timing line (no progress animation)."""
    from xscore.shared.terminal_ui import format_duration, info_line

    t0 = time.perf_counter()
    result = fn()
    info_line(f"{label} · {format_duration(time.perf_counter() - t0)}")
    return result


def detect_blank_page_lists(
    input_path: Path | str,
    *,
    blank_mean: float = BLANK_MEAN_THRESHOLD,
    blank_std: float = BLANK_STD_THRESHOLD,
) -> tuple[int, list[int], list[int], list[tuple[int, int]]]:
    """Raster at :data:`BLANK_DPI`, classify pages; return counts and render sizes per page."""
    input_path = Path(input_path)

    if not input_path.exists():
        raise FileNotFoundError(f"Input file not found: {input_path}")

    _tc = os.cpu_count() or 4
    low_res_pages = _raster_timed(
        f"Blank detection ({BLANK_DPI} DPI)",
        lambda: convert_from_path(
            str(input_path), dpi=BLANK_DPI, grayscale=True, thread_count=_tc
        ),
    )
    try:
        total_pages = len(low_res_pages)
        content_page_nums: list[int] = []
        blank_page_nums: list[int] = []

        def _classify(args: tuple[int, Image.Image]) -> tuple[int, bool]:
            i, page_img = args
            return i + 1, is_blank_page(page_img, blank_mean, blank_std)

        workers = min(4, os.cpu_count() or 1)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            results = list(ex.map(_classify, enumerate(low_res_pages)))
        for page_num, blank in results:
            if blank:
                blank_page_nums.append(page_num)
            else:
                content_page_nums.append(page_num)
        page_render_sizes: list[tuple[int, int]] = [img.size for img in low_res_pages]
    finally:
        del low_res_pages
    return total_pages, content_page_nums, blank_page_nums, page_render_sizes


def write_rotated_pdf_after_blanks(
    input_path: Path | str,
    output_path: Path | str,
    *,
    total_pages: int,
    content_page_nums: list[int],
    blank_page_nums: list[int],
) -> None:
    """Copy *input_path* to *output_path* keeping only ``content_page_nums``.

    ``/Rotate`` metadata is preserved verbatim — step 4 already detected and
    applied per-file orientation, so this step must not influence or overwrite
    the result. The pikepdf round-trip is retained (rather than ``shutil.copy``)
    so the file is structurally normalized as before.
    """
    input_path = Path(input_path)
    output_path = Path(output_path)

    from xscore.shared.terminal_ui import err_line, ok_line, warn_line

    if input_path.resolve() == output_path.resolve():
        err_line(
            "Input and output paths are the same — refusing to overwrite the source PDF. "
            "Choose a different output path."
        )
        raise RuntimeError(
            "Input and output paths are the same — refusing to overwrite the source PDF."
        )

    if not content_page_nums:
        warn_line("All pages were removed (all blank?). Nothing to save.")
        raise RuntimeError("All pages were detected as blank; nothing to save.")

    with pikepdf.open(str(input_path)) as src_pdf:
        out_pdf = pikepdf.new()
        try:
            for pn in content_page_nums:
                out_pdf.pages.append(src_pdf.pages[pn - 1])
            out_pdf.save(str(output_path))
        finally:
            out_pdf.close()

    kept = len(content_page_nums)
    blanks = len(blank_page_nums)
    if blanks:
        page_s = f"{kept} of {total_pages} pages  ·  {blanks} blank pages dropped"
    elif kept == total_pages:
        page_s = f"{total_pages} pages retained"
    else:
        page_s = f"{kept} pages  ·  no blanks"
    ok_line(page_s)


def scan_blanks_state_to_json(
    *,
    source_pdf: Path,
    total_pages: int,
    content_page_nums: list[int],
    blank_page_nums: list[int],
    page_render_sizes: list[tuple[int, int]],
    blank_mean: float,
    blank_std: float,
    analysis_dpi: int,
) -> str:
    """Serialize blank-detection state for the phased scan pipeline (detect_blank_pages → autorotate)."""
    data = {
        "schema_version": 1,
        "source_pdf": str(source_pdf.resolve()),
        "total_pages": total_pages,
        "content_page_nums": content_page_nums,
        "blank_page_nums": blank_page_nums,
        "page_render_sizes": [list(s) for s in page_render_sizes],
        "blank_mean": blank_mean,
        "blank_std": blank_std,
        "analysis_dpi": analysis_dpi,
    }
    return json.dumps(data, indent=2)


def scan_blanks_state_from_json(text: str) -> dict:
    data = json.loads(text)
    if data.get("schema_version") != 1:
        raise ValueError("Unsupported scan_blanks.json schema_version")
    sizes_raw = data["page_render_sizes"]
    page_render_sizes = [tuple(int(x) for x in pair) for pair in sizes_raw]
    data["page_render_sizes"] = page_render_sizes
    return data
