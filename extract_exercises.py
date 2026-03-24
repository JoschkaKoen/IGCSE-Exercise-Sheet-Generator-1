#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Entry point for the exercise extractor (implementation lives in the ``extract_exercises`` package).

Environment (recommended):
    cd "/path/to/Exercise Sheet Generator"
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    source .venv/bin/activate

Usage:
    python extract_exercises.py "Winter 2024 Physics paper 21, questions 12–14, include mark scheme"
    python extract_exercises.py <input_pdf> <output_pdf> <question_numbers...> [--ms <mark_scheme.pdf>]

Web UI (local browser; keep the terminal open while using it):
    source .venv/bin/activate
    uvicorn web.app:app --reload --host 127.0.0.1 --port 8001
    # Then open http://127.0.0.1:8001 in a browser (match the --port you use).
    # If binding fails with "Address already in use", try another port (e.g. 8002);
    # port 8000 is often taken by Docker on macOS.

See ``extract_exercises/__init__.py`` and module docstrings for behaviour details.
"""

from extract_exercises.cli import main

if __name__ == "__main__":
    main()
