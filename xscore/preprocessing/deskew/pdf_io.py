"""PDF batch entry points: ``deskew_pdf_raster`` (main) and the post-cleanup
anchor matcher ``detect_page_anchors_for_cleaned_scan``.
"""

from __future__ import annotations

import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import fitz  # PyMuPDF
import numpy as np
from pdf2image import convert_from_path

from xscore.config import PIPELINE_DEFAULT_DPI
from xscore.preprocessing.deskew.anchors import (
    detect_igcse_anchors, extract_igcse_template,
)
from xscore.preprocessing.deskew.page import _process_page
from xscore.preprocessing.deskew.types import (
    _A3_HEIGHT_THRESHOLD_FACTOR, AnchorPoint,
)


def detect_page_anchors_for_cleaned_scan(
    cleaned_pdf: Path,
    sidecar_path: Path,
    dpi: int,
) -> None:
    """Template-match IGCSE headers on *cleaned_pdf* and write into *sidecar_path*.

    Expects JSON from :func:`deskew_pdf_raster` with ``anchors`` keys present and
    values ``null``. Updates the file in place (pipeline step 8).
    """
    cleaned_pdf = Path(cleaned_pdf)
    sidecar_path = Path(sidecar_path)
    if not cleaned_pdf.is_file():
        raise FileNotFoundError(f"Missing cleaned scan: {cleaned_pdf}")
    if not sidecar_path.is_file():
        raise FileNotFoundError(f"Missing anchor sidecar: {sidecar_path}")

    from xscore.shared.terminal_ui import format_duration, info_line, ok_line

    data: list[dict] = json.loads(sidecar_path.read_text(encoding="utf-8"))
    n_side = len(data)
    if n_side == 0:
        return

    _pdf_kw = dict(
        dpi=dpi,
        grayscale=True,
        thread_count=os.cpu_count() or 4,
    )
    t0 = time.perf_counter()
    pages = convert_from_path(str(cleaned_pdf), **_pdf_kw)
    ok_line(f"Pages loaded · {format_duration(time.perf_counter() - t0)}")

    n = len(pages)
    if n < n_side:
        raise RuntimeError(
            f"Sidecar lists {n_side} pages but PDF rendered {n} — cannot match anchors."
        )

    page0_gray = np.array(pages[0].convert("L"))
    p0_mid = page0_gray.shape[0] // 2
    igcse_template = extract_igcse_template(page0_gray[:p0_mid, :])

    info_line("Matching IGCSE headers …")
    t1 = time.perf_counter()

    def _anc_dict(a: AnchorPoint | None) -> dict | None:
        return asdict(a) if a is not None else None

    def _detect_for_page(i: int) -> tuple[int, dict]:
        page_gray = np.array(pages[i].convert("L"))
        p_mid = page_gray.shape[0] // 2
        tl, tr = detect_igcse_anchors(page_gray[:p_mid, :], igcse_template)
        bl, br = detect_igcse_anchors(page_gray[p_mid:, :], igcse_template)
        return i, {
            "top_left": _anc_dict(tl),
            "top_right": _anc_dict(tr),
            "bot_left": _anc_dict(bl),
            "bot_right": _anc_dict(br),
        }

    workers = min(4, os.cpu_count() or 1)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, anchors in ex.map(_detect_for_page, range(n_side)):
            data[i]["anchors"] = anchors
    sidecar_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    ok_line(f"Anchors saved · {format_duration(time.perf_counter() - t1)}")


def deskew_pdf_raster(
    input_pdf: Path,
    output_pdf: Path,
    dpi: int = PIPELINE_DEFAULT_DPI,
    *,
    reflines_sidecar: Path | None = None,
    saved_as: str | None = None,
) -> Path:
    """Rasterize *input_pdf*, deskew each page (per half), write *output_pdf* and
    a sidecar JSON (``*_anchors.json``) with reflines and **null** IGCSE anchors.

    Call :func:`detect_page_anchors_for_cleaned_scan` after the cleaned PDF exists
    to fill anchor positions (pipeline step 8).

    **Input and output paths must differ** — raw or intermediate PDFs must never
    be overwritten in-place (the pipeline reads the whole file while building
    the new document).  Callers that want to replace an existing file should write
    to a sibling temp path and ``Path.replace`` afterward.

    Args:
        input_pdf: Source PDF after :func:`preprocessing.remove_blanks_autorotate.process_pdf`
            (blanks removed; rotation from PDF ``/Rotate`` and optionally Tesseract OSD).
        output_pdf: Destination PDF (must not resolve to the same path as *input_pdf*).
        dpi: Render/output DPI.
        reflines_sidecar: Optional path for the sidecar JSON (reflines + placeholder
            anchors).  Defaults to :func:`anchors_sidecar_path` applied to
            *output_pdf*.  Use this when *output_pdf* is a temp file but the sidecar
            should use the final stem (e.g. ``cleaned_scan_anchors.json``).
        saved_as: If set, compact-mode success line shows this filename (e.g. final
            ``cleaned_scan.pdf``) when *output_pdf* is a temp path.

    Returns:
        Path to the written output PDF.
    """
    input_pdf = Path(input_pdf)
    output_pdf = Path(output_pdf)

    in_r = input_pdf.resolve()
    out_r = output_pdf.resolve()
    if in_r == out_r:
        raise ValueError(
            "[deskew] input_pdf and output_pdf must be different paths — "
            "refusing to overwrite the source PDF. Write to a temp file, "
            "then Path.replace() if you need to update the original path."
        )

    from xscore.shared.terminal_ui import format_duration, info_line, ok_line
    from xscore.config import CLEANED_SCAN_EMBED_FORMAT, CLEANED_SCAN_JPEG_QUALITY
    from PIL import Image

    with fitz.open(str(input_pdf)) as _src:
        n = _src.page_count
    num_workers = min(os.cpu_count() or 4, n)

    # One pass per page: fitz-render → deskew → JPEG/PNG-encode inside the worker,
    # so each worker only ever holds 1 PIL grayscale + 1 deskewed PIL + 1 buffer
    # (~18 MB) instead of the previous design that kept two full-cohort PIL lists
    # plus an encoded-bytes list alive simultaneously.
    def _process_one(i: int) -> tuple:
        with fitz.open(str(input_pdf)) as _d:
            pix = _d[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        input_h_px = pix.height
        pil_in = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        pix = None  # release pixmap before deskew

        (_idx, deskewed, top_angle, bot_angle, top_lines, bot_lines,
         top_iters, bot_iters, top_method, bot_method) = _process_page((i, pil_in, dpi))
        pil_in = None  # release input PIL once deskew is done

        # Preserve the cleaned_scan.pdf "single embedded JPEG per page at known
        # DPI" contract that the marking fast-path at mark_page.py:_render_page_b64
        # depends on — must stay PIL.save(...) → page.insert_image(stream=...).
        buf = io.BytesIO()
        if CLEANED_SCAN_EMBED_FORMAT == "png":
            deskewed.save(buf, format="PNG")
        else:
            deskewed.save(buf, format="JPEG", quality=CLEANED_SCAN_JPEG_QUALITY)
        return (
            i, buf.getvalue(), deskewed.size, input_h_px,
            top_angle, bot_angle, top_lines, bot_lines,
            top_iters, bot_iters, top_method, bot_method,
        )

    t_angle = time.perf_counter()
    results: dict[int, tuple] = {}
    with ThreadPoolExecutor(max_workers=num_workers) as ex:
        futures = {ex.submit(_process_one, i): i for i in range(n)}
        for fut in as_completed(futures):
            t = fut.result()
            results[t[0]] = t
    angle_elapsed = time.perf_counter() - t_angle

    # Per-page angle log (in page order). `(Ni,method)` shows the number of
    # refinement iterations and which signal was used: "wl" (writing lines,
    # the primary path) or "proj" (projection-variance fallback for pages
    # without detectable answer-line structure).
    for i in range(n):
        (_, _, _, input_h_px,
         top_angle, bot_angle, _, _,
         top_iters, bot_iters, top_method, bot_method) = results[i]
        is_a3 = input_h_px > _A3_HEIGHT_THRESHOLD_FACTOR * dpi
        if is_a3:
            info_line(
                f"Page {i+1:>2} · top {top_angle:+.2f}° ({top_iters}i,{top_method})  "
                f"bot {bot_angle:+.2f}° ({bot_iters}i,{bot_method})"
            )
        else:
            info_line(f"Page {i+1:>2} · {top_angle:+.2f}° ({top_iters}i,{top_method})")
    ok_line(f"Correcting angles · {format_duration(angle_elapsed)}")

    # Build sidecar JSON only when an explicit path is requested.
    # (Step 8 anchor detection is not part of the current pipeline.)
    if reflines_sidecar is not None:
        null_anchors = {
            "top_left": None,
            "top_right": None,
            "bot_left": None,
            "bot_right": None,
        }
        reflines_data: list[dict] = []
        for i in range(n):
            (_, _, _, _, _, _, top_lines, bot_lines, _, _, _, _) = results[i]
            reflines_data.append({
                "page": i + 1,
                "top": [asdict(ln) for ln in top_lines],
                "bot": [asdict(ln) for ln in bot_lines],
                "anchors": dict(null_anchors),
            })
        Path(reflines_sidecar).resolve().write_text(json.dumps(reflines_data, indent=2), encoding="utf-8")

    t_write = time.perf_counter()
    pt_per_px = 72.0 / dpi
    with fitz.open() as doc:
        for i in range(n):
            _, stream_bytes, (w_px, h_px), *_rest = results[i]
            page = doc.new_page(width=w_px * pt_per_px, height=h_px * pt_per_px)
            rect = fitz.Rect(0, 0, w_px * pt_per_px, h_px * pt_per_px)
            page.insert_image(rect, stream=stream_bytes)
            results[i] = None  # free encoded bytes once embedded
        # Image streams (JPEG / PNG) are already compressed; skip extra document-level zlib.
        doc.save(str(output_pdf), deflate=False)

    ok_line(f"Saved scan · {format_duration(time.perf_counter() - t_write)}")
    return output_pdf
