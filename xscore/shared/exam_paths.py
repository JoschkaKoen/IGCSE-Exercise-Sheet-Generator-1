"""Paths for per-exam derived artifacts (under ``output/<stem>/`` by default)."""

from __future__ import annotations

from pathlib import Path


def safe_path_stem(stem: str) -> str:
    """Stable directory / filename fragment from a PDF stem (no spaces or slashes)."""
    return stem.replace(" ", "_").replace("/", "_")


def exam_artifact_dir(exam_folder: Path, output_base: str | Path = "output/xscore") -> Path:
    """Directory for cleaned scans, scaffold cache, images, and debug PDFs.

    *exam_folder* is the exam input directory (raw PDFs, roster). *stem* is the
    folder name with spaces replaced by underscores. Artifacts live under
    ``output/xscore/<stem>/`` by default.
    """
    stem = exam_folder.name.replace(" ", "_")
    return Path(output_base) / stem


def artifact_scaffold_json_path(artifact_dir: Path) -> Path:
    """Canonical report JSON in the run folder."""
    return artifact_dir / "6_report.json"


def artifact_scaffold_markdown_path(artifact_dir: Path) -> Path:
    """Human-readable report beside :func:`artifact_scaffold_json_path`."""
    return artifact_dir / "6_report.md"


def legacy_flat_artifact_scaffold_cache_path(artifact_dir: Path) -> Path:
    """Deprecated: older runs stored the cache as ``scaffold_cache.json`` in the run folder."""
    return artifact_dir / "scaffold_cache.json"


def legacy_artifact_scaffold_cache_path(artifact_dir: Path) -> Path:
    """Older layout: cache lived under ``scaffolds/`` inside *artifact_dir*."""
    return artifact_dir / "scaffolds" / "scaffold_cache.json"


def artifact_scaffold_boxes_path(artifact_dir: Path) -> Path:
    """Vector-exam PDF with scaffold rectangles drawn (one file per run)."""
    return artifact_dir / "1_exam_bboxes.pdf"


def artifact_students_json_path(artifact_dir: Path) -> Path:
    """Step 3: student roster as a JSON array of name strings."""
    return artifact_dir / "3_students.json"


def artifact_students_markdown_path(artifact_dir: Path) -> Path:
    """Step 3: human-readable numbered student list."""
    return artifact_dir / "3_students.md"


def artifact_exam_questions_json_path(artifact_dir: Path) -> Path:
    """Step 4: raw Gemini exam-parse output (no answers/criteria yet)."""
    return artifact_dir / "4_exam_questions.json"


def artifact_exam_questions_markdown_path(artifact_dir: Path) -> Path:
    """Step 4: human-readable exam questions without mark-scheme annotations."""
    return artifact_dir / "4_exam_questions.md"


def artifact_mark_scheme_json_path(artifact_dir: Path) -> Path:
    """Step 5: raw Gemini mark-scheme output before merge into question tree."""
    return artifact_dir / "5_mark_scheme.json"


def artifact_mark_scheme_markdown_path(artifact_dir: Path) -> Path:
    """Step 5: human-readable mark scheme (per-question sections with criteria)."""
    return artifact_dir / "5_mark_scheme.md"


def extract_answers_output_dir(
    pdf_stem: str, output_base: str | Path = "output"
) -> Path:
    """Directory for one ``extract_answers`` run: ``output/extract_answers/<safe_stem>/``."""
    return Path(output_base) / "extract_answers" / safe_path_stem(pdf_stem)


CLEANED_SCAN_PDF = "3_cleaned_scan.pdf"


def find_latest_cleaned_scan(
    exam_folder: Path,
    output_base: str | Path = "output/xscore",
) -> Path | None:
    """Return the newest ``3_cleaned_scan.pdf`` among known layouts, or ``None``.

    Searches (candidates from newest to oldest by mtime):

    - ``<output_base>/<safe_stem>/CLEANED_SCAN_PDF`` (flat)
    - ``<output_base>/<safe_stem>/*/CLEANED_SCAN_PDF`` (per-run folders)
    - Same two patterns under ``output/<safe_stem>/`` (legacy pre-split location)
    - ``<exam_folder>/CLEANED_SCAN_PDF`` (legacy next to exam inputs)

    *safe_stem* is ``exam_folder.name`` with spaces replaced by underscores.
    The winner is the path with the largest ``st_mtime``.
    """
    stem = exam_folder.name.replace(" ", "_")
    name = CLEANED_SCAN_PDF
    candidates: list[Path] = []

    for base in (Path(output_base), Path("output")):   # new location, then legacy
        b = base / stem
        flat = b / name
        if flat.is_file():
            candidates.append(flat)
        if b.is_dir():
            for p in b.glob(f"*/{name}"):
                if p.is_file():
                    candidates.append(p)

    legacy = exam_folder / name
    if legacy.is_file():
        candidates.append(legacy)

    if not candidates:
        return None

    return max(candidates, key=lambda p: p.stat().st_mtime)


def find_scaffold_cache_file(
    exam_folder: Path, output_base: str | Path = "output/xscore"
) -> Path | None:
    """First existing scaffold cache: artifact dir, then legacy locations under *exam_folder*.

    Checks ``output_base/<stem>/`` first, then the legacy ``output/<stem>/`` tree so
    runs created before the output-folder split are still found.
    """
    for base in (output_base, "output"):   # new location first, then legacy
        ad = exam_artifact_dir(exam_folder, base)
        for p in (
            artifact_scaffold_json_path(ad),          # 6_report.json    (current)
            ad / "6_scaffold.json",                   # renamed this session
            ad / "5_scaffold.json",                   # renamed two sessions ago
            ad / "1_scaffold.json",                   # older legacy name
            legacy_flat_artifact_scaffold_cache_path(ad),
            legacy_artifact_scaffold_cache_path(ad),
        ):
            if p.is_file():
                return p
    for p in (
        exam_folder / "scaffolds" / "scaffold_cache.json",
        exam_folder / "scaffold_cache.json",
    ):
        if p.is_file():
            return p
    return None
