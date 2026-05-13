"""Mark-scheme PDF preprocessing: split-into-pages, rasterize, reuse-on-disk."""

from __future__ import annotations

import os
import re
from pathlib import Path

from xscore.scaffold.scaffold_pdf_split import split_pdf_into_pages
from xscore.shared.exam_paths import artifact_mark_scheme_pages_dir


def split_mark_scheme_into_pages(
    marking_scheme_pdf: Path, artifact_dir: "Path | None"
) -> tuple[int, list[Path], "Path | None"]:
    """Split *marking_scheme_pdf* into single-page PDFs under build_marking_register_v1's pages dir.

    Returns ``(n_pages, page_paths, tmp_dir)``. ``tmp_dir`` is non-None only when
    ``artifact_dir`` is None (caller is responsible for cleanup).
    """
    pages_dir = (
        artifact_mark_scheme_pages_dir(artifact_dir) if artifact_dir is not None else None
    )
    return split_pdf_into_pages(marking_scheme_pdf, pages_dir)


def _rasterize_scheme_pages(marking_scheme_pdf: Path, n_pages: int) -> dict[int, bytes]:
    """Rasterize each mark scheme page to PNG (DPI controlled by MARK_SCHEME_GRAPHICS_DPI)."""
    import fitz
    _gfx_dpi = int(os.environ.get("MARK_SCHEME_GRAPHICS_DPI", "300"))
    page_pngs: dict[int, bytes] = {}
    with fitz.open(str(marking_scheme_pdf)) as _doc_r:
        for _i in range(n_pages):
            pix = _doc_r[_i].get_pixmap(dpi=_gfx_dpi)
            page_pngs[_i + 1] = pix.tobytes("png")
            pix = None  # release pixmap memory
    return page_pngs


def _ensure_scheme_pages(
    marking_scheme_pdf: Path, artifact_dir: "Path | None",
) -> tuple[int, list[Path], "Path | None"]:
    """Reuse extract_exam_question_numbers per-page splits if present on disk; otherwise create them.

    Returns ``(n_pages, page_paths, tmp_dir)`` matching ``split_mark_scheme_into_pages``.
    Caller cleans up ``tmp_dir`` (non-None only when ``artifact_dir`` is None).
    """
    if artifact_dir is not None:
        pages_dir = artifact_mark_scheme_pages_dir(artifact_dir)
        if pages_dir.is_dir():
            page_paths = sorted(
                pages_dir.glob("page_*.pdf"),
                key=lambda p: int(re.search(r"page_(\d+)\.pdf$", p.name).group(1)),
            )
            if page_paths:
                return len(page_paths), page_paths, None
    return split_mark_scheme_into_pages(marking_scheme_pdf, artifact_dir)
