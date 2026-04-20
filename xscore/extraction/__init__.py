"""Answer extraction package: profiles, providers, reporting."""

from __future__ import annotations

__version__ = "0.2"

from xscore.extraction.providers import call_ocr_api, get_provider

__all__ = [
    "__version__",
    "call_ocr_api",
    "get_provider",
]
