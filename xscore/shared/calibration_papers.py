"""Canonical list of papers used to calibrate the writing-area detector.

Shared by :mod:`scripts.calibrate_writing_areas` (which renders overlay PDFs for
visual inspection) and the snapshot regression test under
``tests/regression/test_writing_areas_snapshot.py`` (which locks the detector's
output across each paper bit-for-bit).

Each entry is ``(subject_tag, relative_pdf_path_under_exams/)``.  The subject
tag is used as the overlay PDF filename stem and the snapshot golden filename;
the path is resolved against ``exams/`` at the repo root.

Math 0580 papers are all written-answer; sciences 0610/0620/0625 papers 1-2 are
MCQ-only and 3-6 are theory/practical; pick paper 4x (extended theory) for
sciences.
"""

from __future__ import annotations


PAPERS: list[tuple[str, str]] = [
    ("mathematics",            "mathematics/0580 Mathematics March 2025 Question Paper  12.pdf"),
    ("mathematics_22",         "mathematics/0580 Mathematics March 2025 Question Paper  22.pdf"),
    ("physics",                "physics/0625 Physics November 2025 Question Paper  42.pdf"),
    ("biology",                "biology/0610 Biology June 2021 Question Paper  42.pdf"),
    ("biology_32",             "biology/0610 Biology June 2021 Question Paper  32.pdf"),
    ("biology_22",             "biology/0610 Biology June 2021 Question Paper  22.pdf"),
    ("biology_62",             "biology/0610 Biology June 2021 Question Paper  62.pdf"),
    ("chemistry",              "chemistry/0620 Chemistry June 2021 Question paper  42.pdf"),
    ("computer_science",       "computer_science/0478_m20_qp_22.pdf"),
    ("a_level_biology",        "a_level_biology/9700 Biology 2022 Specimen Question Paper  3.pdf"),
    ("a_level_chemistry",      "a_level_chemistry/9701 Chemistry 2022 Specimen Question Paper  3.pdf"),
    ("a_level_physics",        "a_level_physics/9702 Physics 2022 Specimen Question Paper  3.pdf"),
    ("a_level_computer_science", "a_level_computer_science/9618 Computer Science 2021 Specimen Question Paper  2.pdf"),
]
