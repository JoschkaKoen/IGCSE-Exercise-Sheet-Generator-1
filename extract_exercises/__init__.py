# -*- coding: utf-8 -*-
"""
IGCSE question-paper extraction: raster regions, layout to PDF, optional mark schemes.

Use the CLI via ``python extract_exercises.py`` or ``python -m extract_exercises``.
"""

from .labels import (
    build_exam_header_label,
    build_exam_header_label_from_paths,
    exam_label_from_filename,
    paper_label_from_qp_path,
)
from .pipeline import run_extraction, run_extraction_jobs

__all__ = [
    "build_exam_header_label",
    "build_exam_header_label_from_paths",
    "exam_label_from_filename",
    "paper_label_from_qp_path",
    "run_extraction",
    "run_extraction_jobs",
]
