"""Canonical list of papers used to calibrate the writing-area detector.

Shared by :mod:`scripts.calibrate_writing_areas` (which renders overlay PDFs for
visual inspection) and the snapshot regression test under
``scripts/verify_writing_areas_snapshot.py`` (which locks the detector's output
across each paper bit-for-bit).

Each entry is ``(subject_tag, relative_pdf_path_under_exams/)``.  The subject
tag is used as the overlay PDF filename stem and the snapshot golden filename;
the path is resolved against ``exams/`` at the repo root.

**Coverage goal:** at least one paper per paper-type per subject, so the
detector is exercised against every Cambridge layout family in our library.

IGCSE numbered papers run 1-6 (paper 1-2 multiple-choice, 3-4 written theory,
5-6 practical / alternative-to-practical).  A-level numbered papers run 1-5
(paper 1 multiple-choice, 2-4 written theory, 5 planning).  Computer Science
(IGCSE and A-level) has fewer paper types.

Math 0580 papers are all written-answer; sciences 0610/0620/0625 papers 1-2 are
MCQ-only and 3-6 are theory/practical; pick paper 4x (extended theory) for
sciences when sampling a single representative.
"""

from __future__ import annotations


PAPERS: list[tuple[str, str]] = [
    # === IGCSE Mathematics 0580 (paper types 1-4) ===
    ("mathematics",            "mathematics/0580 Mathematics March 2025 Question Paper  12.pdf"),
    ("mathematics_22",         "mathematics/0580 Mathematics March 2025 Question Paper  22.pdf"),
    ("mathematics_32",         "mathematics/0580 Mathematics June 2025 Question Paper  32.pdf"),
    ("mathematics_43",         "mathematics/0580 Mathematics November 2021 Question Paper  43.pdf"),

    # === IGCSE Physics 0625 (paper types 1-6) ===
    ("physics_13",             "physics/0625_s24_qp_13.pdf"),
    ("physics_23",             "physics/0625 Physics November 2025 Question Paper  23.pdf"),
    ("physics_32",             "physics/0625_m23_qp_32.pdf"),
    ("physics",                "physics/0625 Physics November 2025 Question Paper  42.pdf"),
    ("physics_51",             "physics/0625_w24_qp_51.pdf"),
    ("physics_63",             "physics/0625_w21_qp_63.pdf"),

    # === IGCSE Biology 0610 (paper types 1-6) ===
    ("biology_13",             "biology/0610 Biology November 2023 Question Paper  13.pdf"),
    ("biology_22",             "biology/0610 Biology June 2021 Question Paper  22.pdf"),
    ("biology_32",             "biology/0610 Biology June 2021 Question Paper  32.pdf"),
    ("biology",                "biology/0610 Biology June 2021 Question Paper  42.pdf"),
    ("biology_53",             "biology/0610 Biology June 2023 Question Paper  53.pdf"),
    ("biology_62",             "biology/0610 Biology June 2021 Question Paper  62.pdf"),

    # === IGCSE Chemistry 0620 (paper types 1-6) ===
    ("chemistry_12",           "chemistry/0620 Chemistry November 2022 Question paper  12.pdf"),
    ("chemistry_21",           "chemistry/0620 Chemistry June 2021 Question paper  21.pdf"),
    ("chemistry_33",           "chemistry/0620 Chemistry June 2025 Question Paper  33.pdf"),
    ("chemistry",              "chemistry/0620 Chemistry June 2021 Question paper  42.pdf"),
    ("chemistry_52",           "chemistry/0620 Chemistry June 2024 Question paper  52.pdf"),
    ("chemistry_61",           "chemistry/0620 Chemistry November 2023 Question paper  61.pdf"),

    # === IGCSE Computer Science 0478 (paper types 1-2) ===
    ("computer_science_12",    "computer_science/0478_s21_qp_12.pdf"),
    ("computer_science",       "computer_science/0478_m20_qp_22.pdf"),

    # === A-level Biology 9700 (paper types 1-5) ===
    ("a_level_biology_12",        "a_level_biology/9700 Biology November 2025 Question Paper  12.pdf"),
    ("a_level_biology_23",        "a_level_biology/9700 Biology June 2025 Question Paper  23.pdf"),
    ("a_level_biology",           "a_level_biology/9700 Biology 2022 Specimen Question Paper  3.pdf"),
    ("a_level_biology_42",        "a_level_biology/9700 Biology March 2025 Question paper  42.pdf"),
    ("a_level_biology_53",        "a_level_biology/9700 Biology June 2023 Question paper  53.pdf"),

    # === A-level Chemistry 9701 (paper types 1-5) ===
    ("a_level_chemistry_12",      "a_level_chemistry/9701 Chemistry June 2025 Question Paper  12.pdf"),
    ("a_level_chemistry_23",      "a_level_chemistry/9701 Chemistry November 2023 Question paper  23.pdf"),
    ("a_level_chemistry",         "a_level_chemistry/9701 Chemistry 2022 Specimen Question Paper  3.pdf"),
    ("a_level_chemistry_41",      "a_level_chemistry/9701 Chemistry November 2021 Question paper  41.pdf"),
    ("a_level_chemistry_53",      "a_level_chemistry/9701 Chemistry November 2025 Question Paper  53.pdf"),

    # === A-level Physics 9702 (paper types 1-5) ===
    ("a_level_physics_11",        "a_level_physics/9702 Physics June 2021 Question paper  11.pdf"),
    ("a_level_physics_23",        "a_level_physics/9702 Physics November 2023 Question paper  23.pdf"),
    ("a_level_physics",           "a_level_physics/9702 Physics 2022 Specimen Question Paper  3.pdf"),
    ("a_level_physics_41",        "a_level_physics/9702 Physics June 2025 Question Paper  41.pdf"),
    ("a_level_physics_53",        "a_level_physics/9702 Physics June 2021 Question paper  53.pdf"),

    # === A-level Computer Science 9618 (paper types 1-4) ===
    ("a_level_computer_science_12",   "a_level_computer_science/9618 Computer Science November 2023 Question paper  12.pdf"),
    ("a_level_computer_science",      "a_level_computer_science/9618 Computer Science 2021 Specimen Question Paper  2.pdf"),
    ("a_level_computer_science_33",   "a_level_computer_science/9618 Computer Science June 2022 Question paper  33.pdf"),
    ("a_level_computer_science_43",   "a_level_computer_science/9618 Computer Science June 2024 Question paper  43.pdf"),
]
