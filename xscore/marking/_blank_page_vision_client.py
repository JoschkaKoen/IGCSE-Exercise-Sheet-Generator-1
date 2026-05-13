"""Shared infrastructure for the empty-exam classifier (classify_empty_exam_pages) and the
student-handwriting check (student_handwriting_check).

These two steps share:

- a small page-image rendering helper (single PDF page → JPEG bytes, or →
  single-page PDF bytes for Gemini's native PDF path),
- a thin client-state object that holds either a Gemini native client or an
  OpenAI-compatible client (whichever the configured model dispatches to),
- the ``BlankCheckStatus`` enum used by both public entry points to signal
  PASSED / INCONCLUSIVE without raising.

Refactored out of the monolithic ``blank_page_detection`` module so the two
step bodies can live in dedicated files without duplicating boilerplate.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any


class BlankCheckStatus(Enum):
    PASSED = "PASSED"
    INCONCLUSIVE = "INCONCLUSIVE"


# ─────────── Image extraction ───────────────────────────────────────────────

HANDWRITING_JPEG_DPI = 150
HANDWRITING_JPEG_QUALITY = 75  # PyMuPDF default for tobytes("jpeg") — explicit so it's announceable


def _render_page_jpeg(
    pdf_path: Path,
    page_1based: int,
    dpi: int = HANDWRITING_JPEG_DPI,
    quality: int = HANDWRITING_JPEG_QUALITY,
) -> bytes:
    import fitz
    with fitz.open(str(pdf_path)) as doc:
        pix = doc[page_1based - 1].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    return pix.tobytes("jpeg", jpg_quality=quality)


def _extract_page_as_pdf_bytes(pdf_path: Path, page_1based: int) -> bytes:
    """Extract one page out of *pdf_path* as a self-contained single-page PDF.

    Used by the classify_empty_exam_pages empty-exam classifier on the Gemini path so each
    parallel call sees exactly one page (rather than the whole exam) without
    rasterizing the vector PDF first.
    """
    import io

    import fitz

    with fitz.open(str(pdf_path)) as src:
        out = fitz.open()
        try:
            out.insert_pdf(src, from_page=page_1based - 1, to_page=page_1based - 1)
            buf = io.BytesIO()
            out.save(buf)
            return buf.getvalue()
        finally:
            out.close()


# ─────────── Model client (shared by both helpers) ───────────────────────────

class _ClientState:
    def __init__(self, gai: Any, oa: Any, provider: str | None) -> None:
        self.gai = gai
        self.oa = oa
        self.provider = provider


def _build_client_state(model_id: str) -> _ClientState | str:
    """Return ``_ClientState`` on success, or a human-readable error message string."""
    if model_id.startswith("gemini"):
        from eXercise.ai_client import make_gemini_native_client
        gai = make_gemini_native_client()
        if gai is None:
            return "GEMINI_API_KEY not set"
        return _ClientState(gai=gai, oa=None, provider="gemini")
    from eXercise.ai_client import make_ai_client
    result = make_ai_client(model_env="", default_model=model_id)
    if result is None:
        return f"model={model_id} requires API key for its provider"
    oa, _, provider, _, _ = result
    return _ClientState(gai=None, oa=oa, provider=provider)


# ─────────── Response-parser helper ─────────────────────────────────────────

def _coerce_conf(v) -> int | None:
    """Coerce a model-returned confidence to an int in [0, 10], or None on garbage."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return max(0, min(10, v))
    if isinstance(v, (float, str)):
        try:
            return max(0, min(10, int(float(str(v).strip()))))
        except (TypeError, ValueError):
            return None
    return None
