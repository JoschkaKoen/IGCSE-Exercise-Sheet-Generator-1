# -*- coding: utf-8 -*-
"""User-facing errors raised by the extraction library (CLI prints and exits; web returns JSON)."""


class ExtractionUserError(Exception):
    """Base class for recoverable errors with a message safe to show the user."""


class NaturalLanguageError(ExtractionUserError):
    """Natural-language resolution failed (missing deps, API, invalid JSON, etc.)."""


class ExtractionError(ExtractionUserError):
    """Extraction jobs could not run or produced no output."""
