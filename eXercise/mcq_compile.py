# -*- coding: utf-8 -*-
"""LaTeX compilation and PDF-to-VectorStrip conversion for MCQ explanations.

Wraps pdflatex invocation and converts the resulting single-page PDFs into
VectorStrip objects suitable for the layout engine.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import fitz

from .mcq_latex import _MARGIN_PT, _USABLE_H_PT, _USABLE_W_PT


def _find_pdflatex() -> str | None:
    """Return path to pdflatex, preferring MacTeX location."""
    candidates = [
        "/Library/TeX/texbin/pdflatex",
        "/usr/local/bin/pdflatex",
        "/usr/bin/pdflatex",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return shutil.which("pdflatex")


def compile_latex(tex_source: str, output_pdf: Path) -> bool:
    """Write *tex_source* to a temp dir, run pdflatex, copy result to *output_pdf*.

    Returns ``True`` on success, ``False`` on failure.
    """
    pdflatex = _find_pdflatex()
    if not pdflatex:
        print("  MCQ explanations: pdflatex not found; falling back to plain text.")
        return False

    with tempfile.TemporaryDirectory(prefix="mcq_expl_") as tmp:
        tmp_path = Path(tmp)
        tex_file = tmp_path / "explanations.tex"
        tex_file.write_text(tex_source, encoding="utf-8")

        cmd = [
            pdflatex,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-output-directory", str(tmp_path),
            str(tex_file),
        ]

        for run in range(2):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    cwd=str(tmp_path),
                )
                if result.returncode != 0 and run == 1:
                    log_snippet = (result.stdout or "")[-1500:]
                    print(f"  MCQ explanations: pdflatex failed (run {run + 1}):\n{log_snippet}")
                    return False
            except subprocess.TimeoutExpired:
                print("  MCQ explanations: pdflatex timed out.")
                return False
            except OSError as exc:
                print(f"  MCQ explanations: pdflatex error: {exc}")
                return False

        compiled = tmp_path / "explanations.pdf"
        if not compiled.is_file():
            print("  MCQ explanations: pdflatex ran but produced no PDF.")
            return False

        shutil.copy2(str(compiled), str(output_pdf))
        return True


def _content_bottom_pt(page: fitz.Page, padding: float = 6.0) -> float:
    """Return the y-coordinate of the bottom of the last piece of content on *page*.

    Scans text blocks and vector drawings (e.g. booktabs rules) to find the
    lowest ink on the page, then adds *padding* points of breathing room.
    The result is capped at the page height so it is always a valid clip limit.
    """
    max_y = 0.0
    for block in page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]:
        max_y = max(max_y, block["bbox"][3])
    for d in page.get_drawings():
        max_y = max(max_y, d["rect"].y1)
    return min(max_y + padding, page.rect.height)


def _pdf_to_vector_strips(
    pdf_path: Path,
    questions_in_order: list[int],
    first_q_num: int | None,
) -> list[Any]:
    """Open *pdf_path* and return one VectorStrip per page, keeping the doc open.

    The returned doc is embedded in the strips and must stay alive as long as the
    strips are used by the layout engine.  The caller (pipeline) holds a reference
    via the returned strips list.

    ``question_num`` is set to ``first_q_num`` on the first strip.
    ``extra_question_nums`` carries the remaining questions so the layout engine
    records answer-navigation anchors for every MCQ question, not just the first.
    """
    from .rendering import VectorStrip  # noqa: PLC0415

    doc = fitz.open(stream=pdf_path.read_bytes(), filetype="pdf")
    extra = [q for q in questions_in_order if q != first_q_num]
    strips: list[Any] = []
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        pr = page.rect
        content_h = _content_bottom_pt(page)

        scale_w = _USABLE_W_PT / pr.width if pr.width > 0 else 1.0
        scale_h = _USABLE_H_PT / content_h if content_h > 0 else 1.0
        scale = min(scale_w, scale_h)
        display_w = pr.width * scale
        display_h = content_h * scale
        strips.append(VectorStrip(
            src_doc=doc,
            page_idx=page_idx,
            clip_rect=fitz.Rect(0, 0, pr.width, content_h),
            display_h_pt=display_h,
            display_w_pt=display_w,
            x_offset_pt=(_USABLE_W_PT - display_w) / 2 + _MARGIN_PT,
            qr_rects=[],
            question_num=first_q_num if page_idx == 0 else None,
            extra_question_nums=extra if page_idx == 0 else [],
        ))
    return strips
