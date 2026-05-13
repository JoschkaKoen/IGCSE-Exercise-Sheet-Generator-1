"""Benchmarking harness for raw AI extraction quality — not used by the grading pipeline."""

from __future__ import annotations

__version__ = "0.6.0"

from xscore.extraction.providers import call_ocr_api, get_provider

__all__ = [
    "__version__",
    "call_ocr_api",
    "get_provider",
]
