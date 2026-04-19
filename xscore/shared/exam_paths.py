"""Paths for per-exam derived artifacts (under ``output/<stem>/`` by default)."""

from __future__ import annotations

from pathlib import Path


SUBDIR_META     = "meta"
SUBDIR_PROMPTS  = "prompts"
SUBDIR_SCAFFOLD = "scaffold"
SUBDIR_SCAN     = "scan"
SUBDIR_MARKING  = "marking"
SUBDIR_REPORTS  = "reports"


def artifact_prompt_path(artifact_dir: Path, name: str) -> Path:
    """Path for a saved AI prompt, routed into a sub-subdirectory by step prefix."""
    if name.startswith("10_name"):
        subdir = "name_detection"
    elif name.startswith("12_"):
        subdir = "marking"
    else:
        subdir = ""
    base = artifact_dir / SUBDIR_PROMPTS
    return (base / subdir / f"{name}.md") if subdir else (base / f"{name}.md")


def safe_path_stem(stem: str) -> str:
    """Stable directory / filename fragment from a PDF stem (no spaces, slashes, or traversal)."""
    # Remove null bytes and replace path-unsafe characters.
    stem = stem.replace("\x00", "").replace(" ", "_").replace("/", "_").replace("\\", "_")
    # Prevent directory traversal by replacing ".." components.
    parts = stem.split("_")
    parts = [p if p != ".." else "__" for p in parts]
    return "_".join(parts) or "_"


def exam_artifact_dir(exam_folder: Path, output_base: str | Path = "output/xscore") -> Path:
    """Directory for cleaned scans, scaffold cache, images, and debug PDFs.

    *exam_folder* is the exam input directory (raw PDFs, roster). *stem* is the
    folder name with spaces replaced by underscores. Artifacts live under
    ``output/xscore/<stem>/`` by default.
    """
    stem = exam_folder.name.replace(" ", "_")
    return Path(output_base) / stem


def artifact_scaffold_yaml_path(artifact_dir: Path) -> Path:
    """Canonical report YAML in the run folder."""
    return artifact_dir / SUBDIR_SCAFFOLD / "6_report.yaml"


def artifact_scaffold_markdown_path(artifact_dir: Path) -> Path:
    """Human-readable report beside :func:`artifact_scaffold_yaml_path`."""
    return artifact_dir / SUBDIR_SCAFFOLD / "6_report.md"


def artifact_short_scaffold_yaml_path(artifact_dir: Path) -> Path:
    """Short report YAML — same as 6_report.yaml but without the student list."""
    return artifact_dir / SUBDIR_SCAFFOLD / "6_short_report.yaml"


def artifact_short_scaffold_markdown_path(artifact_dir: Path) -> Path:
    """Short report markdown — same as 6_report.md but without the student list."""
    return artifact_dir / SUBDIR_SCAFFOLD / "6_short_report.md"


def artifact_scaffold_json_path(artifact_dir: Path) -> Path:
    """Legacy JSON scaffold cache path — kept for backward-compat lookup of old runs."""
    return artifact_dir / SUBDIR_SCAFFOLD / "6_report.json"


def legacy_flat_artifact_scaffold_cache_path(artifact_dir: Path) -> Path:
    """Deprecated: older runs stored the cache as ``scaffold_cache.json`` in the run folder."""
    return artifact_dir / "scaffold_cache.json"


def legacy_artifact_scaffold_cache_path(artifact_dir: Path) -> Path:
    """Older layout: cache lived under ``scaffolds/`` inside *artifact_dir*."""
    return artifact_dir / "scaffolds" / "scaffold_cache.json"


def artifact_scaffold_boxes_path(artifact_dir: Path) -> Path:
    """Vector-exam PDF with scaffold rectangles drawn (one file per run)."""
    return artifact_dir / SUBDIR_SCAFFOLD / "1_exam_bboxes.pdf"


def artifact_students_json_path(artifact_dir: Path) -> Path:
    """Step 3: student roster as a JSON array of name strings."""
    return artifact_dir / SUBDIR_SCAFFOLD / "3_students.json"


def artifact_students_markdown_path(artifact_dir: Path) -> Path:
    """Step 3: human-readable numbered student list."""
    return artifact_dir / SUBDIR_SCAFFOLD / "3_students.md"


def artifact_exam_questions_yaml_path(artifact_dir: Path) -> Path:
    """Step 4: raw Gemini exam-parse output (no answers/criteria yet)."""
    return artifact_dir / SUBDIR_SCAFFOLD / "4_exam_questions.yaml"


def artifact_exam_questions_markdown_path(artifact_dir: Path) -> Path:
    """Step 4: human-readable exam questions without mark-scheme annotations."""
    return artifact_dir / SUBDIR_SCAFFOLD / "4_exam_questions.md"


def artifact_mark_scheme_yaml_path(artifact_dir: Path) -> Path:
    """Step 5: raw Gemini mark-scheme output before merge into question tree."""
    return artifact_dir / SUBDIR_SCAFFOLD / "5_mark_scheme.yaml"


def artifact_mark_scheme_markdown_path(artifact_dir: Path) -> Path:
    """Step 5: human-readable mark scheme (per-question sections with criteria)."""
    return artifact_dir / SUBDIR_SCAFFOLD / "5_mark_scheme.md"


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
        for p in b.glob(f"*/scan/{name}"):
            if p.is_file():
                candidates.append(p)

    legacy = exam_folder / name
    if legacy.is_file():
        candidates.append(legacy)

    if not candidates:
        return None

    return max(candidates, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Steps 10–14: AI marking pipeline artifacts
# ---------------------------------------------------------------------------

def artifact_geometry_json_path(artifact_dir: Path) -> Path:
    """Step 10: exam geometry (page counts, student count)."""
    return artifact_dir / SUBDIR_MARKING / "geometry" / "10_exam_geometry.json"


def artifact_geometry_md_path(artifact_dir: Path) -> Path:
    """Step 10: human-readable exam geometry table."""
    return artifact_dir / SUBDIR_MARKING / "geometry" / "10_exam_geometry.md"


def artifact_exam_student_list_json_path(artifact_dir: Path) -> Path:
    """Step 10: scan-detected student list with page assignments."""
    return artifact_dir / SUBDIR_MARKING / "geometry" / "10_exam_student_list.json"


def artifact_exam_student_list_md_path(artifact_dir: Path) -> Path:
    """Step 10: human-readable student-to-page assignment table."""
    return artifact_dir / SUBDIR_MARKING / "geometry" / "10_exam_student_list.md"


def artifact_blueprint_yaml_path(artifact_dir: Path, page: int) -> Path:
    """Step 11: empty AI marking blueprint for one exam page."""
    return artifact_dir / SUBDIR_MARKING / "blueprints" / f"11_ai_marking_blueprint_{page}.yaml"


def artifact_blueprint_md_path(artifact_dir: Path, page: int) -> Path:
    """Step 11: human-readable blueprint summary for one exam page."""
    return artifact_dir / SUBDIR_MARKING / "blueprints" / f"11_ai_marking_blueprint_{page}.md"


def artifact_marked_path(artifact_dir: Path, student: str, page: int) -> Path:
    """Step 12: AI-filled marking result for one student's scan page (plain Q-block text)."""
    import re
    safe = re.sub(r"[^\w]", "_", student)
    return artifact_dir / SUBDIR_MARKING / "students" / f"12_marked_{safe}_{page}.txt"


def artifact_marked_md_path(artifact_dir: Path, student: str, page: int) -> Path:
    """Step 12: human-readable marking result for one student's scan page."""
    import re
    safe = re.sub(r"[^\w]", "_", student)
    return artifact_dir / SUBDIR_MARKING / "students" / f"12_marked_{safe}_{page}.md"


def artifact_marking_students_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student marking text files (step 12)."""
    return artifact_dir / SUBDIR_MARKING / "students"


def artifact_student_report_yaml_path(artifact_dir: Path, student: str) -> Path:
    """Step 13: merged student report YAML."""
    import re
    safe = re.sub(r"[^\w]", "_", student)
    return artifact_dir / SUBDIR_REPORTS / "students" / f"13_student_report_{safe}.yaml"


def artifact_student_report_md_path(artifact_dir: Path, student: str) -> Path:
    """Step 13: human-readable student report."""
    import re
    safe = re.sub(r"[^\w]", "_", student)
    return artifact_dir / SUBDIR_REPORTS / "students" / f"13_student_report_{safe}.md"


def artifact_student_report_tex_path(artifact_dir: Path, student: str) -> Path:
    """Step 13: LaTeX source for student report PDF."""
    import re
    safe = re.sub(r"[^\w]", "_", student)
    return artifact_dir / SUBDIR_REPORTS / "students" / f"13_student_report_{safe}.tex"


def artifact_reports_students_dir(artifact_dir: Path) -> Path:
    """Directory containing per-student report YAML files (step 13)."""
    return artifact_dir / SUBDIR_REPORTS / "students"


def artifact_class_report_yaml_path(artifact_dir: Path) -> Path:
    """Step 13: class-wide summary YAML."""
    return artifact_dir / SUBDIR_REPORTS / "class" / "13_class_report.yaml"


def artifact_class_report_md_path(artifact_dir: Path) -> Path:
    """Step 13: human-readable class report."""
    return artifact_dir / SUBDIR_REPORTS / "class" / "13_class_report.md"


def artifact_class_report_tex_path(artifact_dir: Path) -> Path:
    """Step 13: LaTeX source for class report PDF."""
    return artifact_dir / SUBDIR_REPORTS / "class" / "13_class_report.tex"


def artifact_class_report_pdf_path(artifact_dir: Path) -> Path:
    """Step 13: compiled class report PDF."""
    return artifact_dir / SUBDIR_REPORTS / "class" / "13_class_report.pdf"


def artifact_timing_json_path(artifact_dir: Path) -> Path:
    """Step 14: marking pipeline timing data."""
    return artifact_dir / SUBDIR_META / "14_timing.json"


def artifact_timing_md_path(artifact_dir: Path) -> Path:
    """Step 14: human-readable timing table."""
    return artifact_dir / SUBDIR_META / "14_timing.md"


def validate_input_files(folder: Path) -> None:
    """Raise FileNotFoundError listing every missing required input file."""
    missing: list[str] = []

    scans = [
        f for f in folder.glob("*.pdf")
        if "scan" in f.name.lower() and "cleaned" not in f.name.lower()
    ]
    if not scans:
        missing.append("scan PDF  (e.g. scan.pdf)")

    if not list(folder.glob("StudentList.*")):
        missing.append("student roster  (StudentList.xlsx / .csv / .pdf)")

    # Accept exact name OR any PDF with 'empty'/'exam' that isn't a scan/answer/student file
    # (mirrors generate_scaffold.find_exam_pdf so validation is consistent with the pipeline)
    _EXAM_SKIP = ("scan", "answer", "student", "cleaned")
    _exam_pdfs = [
        f for f in folder.glob("*.pdf")
        if not any(kw in f.name.lower() for kw in _EXAM_SKIP)
        and any(kw in f.name.lower() for kw in ("empty", "exam"))
    ]
    if not (folder / "empty_exam.pdf").is_file() and not _exam_pdfs:
        missing.append("empty_exam.pdf  (or any *empty*/*exam*.pdf that isn't a scan/answer)")

    # Accept exact name OR any PDF with 'answer' in the name
    # (mirrors generate_scaffold._find_answer_pdf)
    _answer_pdfs = [f for f in folder.glob("*.pdf") if "answer" in f.name.lower()]
    if not (folder / "answer_sheet.pdf").is_file() and not _answer_pdfs:
        missing.append("answer_sheet.pdf  (or any *answer*.pdf)")

    if missing:
        bullet = "\n  • "
        raise FileNotFoundError(
            f"Required input files missing from {folder}:{bullet}{bullet.join(missing)}"
        )


def artifact_accuracy_json_path(artifact_dir: Path) -> Path:
    """Step 14: recognition accuracy vs ground truth."""
    return artifact_dir / SUBDIR_META / "14_accuracy.json"


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
            artifact_scaffold_yaml_path(ad),          # 6_report.yaml    (current)
            artifact_scaffold_json_path(ad),          # 6_report.json    (legacy fallback)
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
