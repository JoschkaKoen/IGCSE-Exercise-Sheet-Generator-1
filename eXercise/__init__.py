# -*- coding: utf-8 -*-
"""
IGCSE question-paper extraction: raster regions, layout to PDF, optional mark schemes.

Use the CLI via ``python eXercise.py`` or ``python -m eXercise``.
"""

from .labels import paper_label_from_qp_path
from .pipeline import run_extraction, run_extraction_jobs

__all__ = [
    "paper_label_from_qp_path",
    "run_extraction",
    "run_extraction_jobs",
]
