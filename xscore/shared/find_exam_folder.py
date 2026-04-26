"""Resolve the exam folder from a CLI override, instruction hint, or heuristic search.

Also exports :func:`validate_input_files` which checks that all required PDFs and the
student roster are present before the pipeline starts.
"""

from __future__ import annotations

import os
from difflib import SequenceMatcher
from pathlib import Path


def find_folder(
    instruction_hint: str | None = None,
    cli_override: str | None = None,
    ai_folder_path: str | None = None,
    search_root: Path | None = None,
) -> Path:
    """Return the exam folder path.

    Priority:
    1. ``cli_override`` (--folder flag) — used as-is, must exist.
    2. ``ai_folder_path`` — explicit path from parsed prompt (same rules as CLI).
    3. ``instruction_hint`` — exact directory name match in ``search_root``.
    4. ``instruction_hint`` — fuzzy match against sub-directory names (≥ 0.6 ratio).
    5. Heuristic fallback: newest directory containing "test" or "exam" (case-insensitive)
       in ``search_root``.

    Raises ``FileNotFoundError`` if nothing is found.
    """
    root = search_root or Path.cwd()

    def _resolve_explicit(path_str: str, label: str) -> Path:
        p = Path(os.path.expandvars(path_str.strip())).expanduser()
        if not p.is_absolute():
            p = root / p
        if p.is_dir():
            return p.resolve()
        raise FileNotFoundError(f"{label} path does not exist or is not a directory: {p}")

    # 1. Explicit CLI override
    if cli_override:
        return _resolve_explicit(cli_override, "--folder")

    # 2. Explicit path from natural-language instruction (AI)
    if ai_folder_path and str(ai_folder_path).strip():
        return _resolve_explicit(str(ai_folder_path), "Prompt-specified folder")

    candidates = [d for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")]

    # 3. Exact name match on hint
    if instruction_hint:
        hint_lower = instruction_hint.strip().lower()
        for d in candidates:
            if d.name.lower() == hint_lower:
                return d.resolve()

    # 4. Fuzzy match on hint
    if instruction_hint:
        hint_lower = instruction_hint.strip().lower()
        best: tuple[float, Path | None] = (0.0, None)
        for d in candidates:
            ratio = SequenceMatcher(None, hint_lower, d.name.lower()).ratio()
            # Also accept substring containment as a strong match
            if hint_lower in d.name.lower() or d.name.lower() in hint_lower:
                ratio = max(ratio, 0.75)
            if ratio > best[0]:
                best = (ratio, d)
        if best[0] >= 0.6 and best[1] is not None:
            return best[1].resolve()

    # 5. Heuristic: newest dir whose name contains "test" or "exam"
    exam_dirs = sorted(
        [d for d in candidates if any(kw in d.name.lower() for kw in ("test", "exam"))],
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )
    if exam_dirs:
        return exam_dirs[0].resolve()

    raise FileNotFoundError(
        f"Could not locate an exam folder in {root}. "
        "Use --folder to specify it explicitly."
    )


def validate_input_files(folder: Path) -> None:
    """Raise FileNotFoundError listing every missing required input file."""
    missing: list[str] = []

    scans = [
        f for f in folder.glob("*.pdf")
        if "scan" in f.name.lower() and "cleaned" not in f.name.lower()
    ]
    if not scans:
        missing.append("scan PDF  (e.g. scan.pdf, or scan1.pdf + scan2.pdf for duplex)")

    _roster = list(folder.glob("StudentList.*"))
    if not _roster:
        for _pat in ("*[Ss]tudent*", "*[Rr]oster*"):
            _roster = list(folder.glob(_pat))
            if _roster:
                break
    if not _roster:
        missing.append("student roster  (StudentList.xlsx, or any *student* / *roster* file)")

    # Accept: exact name, any *empty*/*exam*/Cambridge *_qp_* PDF, or (fallback)
    # any non-scan/answer/student/_ms_ PDF.
    # Mirrors generate_scaffold.find_exam_pdf which uses the same fallback.
    _EXAM_SKIP = ("scan", "answer", "student", "cleaned", "_ms_")
    _non_skip_pdfs = [
        f for f in folder.glob("*.pdf")
        if not any(kw in f.name.lower() for kw in _EXAM_SKIP)
    ]
    _exam_pdfs = [
        f for f in _non_skip_pdfs
        if any(kw in f.name.lower() for kw in ("empty", "exam", "_qp_"))
    ]
    if not (folder / "empty_exam.pdf").is_file() and not _exam_pdfs and not _non_skip_pdfs:
        missing.append(
            "empty_exam.pdf  (or any PDF that isn't a scan/answer/student file; "
            "Cambridge *_qp_*.pdf also accepted)"
        )

    # Accept exact name OR any PDF with 'answer' or Cambridge '_ms_' in the name
    # (mirrors generate_scaffold.find_answer_pdf)
    _answer_pdfs = [
        f for f in folder.glob("*.pdf")
        if "answer" in f.name.lower() or "_ms_" in f.name.lower()
    ]
    if not (folder / "answer_sheet.pdf").is_file() and not _answer_pdfs:
        missing.append("answer_sheet.pdf  (or any *answer*.pdf, or Cambridge *_ms_*.pdf)")

    if missing:
        bullet = "\n  • "
        raise FileNotFoundError(
            f"Required input files missing from {folder}:{bullet}{bullet.join(missing)}"
        )
