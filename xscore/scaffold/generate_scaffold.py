"""Build an :class:`ExamScaffold` for an exam folder.

Top-level orchestration:

- :func:`find_exam_pdf`, :func:`find_answer_pdf` — pick the vector exam and
  answer-key PDFs out of an exam folder.
- :func:`build_scaffold` — load from cache if a fresh cache exists, otherwise
  call :func:`xscore.scaffold.ai_scaffold.build_ai_scaffold` and finalize.
- :func:`finalize_scaffold` — mark rollups, page count, ``ExamScaffold``
  construction, and cache write.

Disk cache serializers (JSON, XML, legacy migrations) live in
:mod:`xscore.scaffold.scaffold_cache`.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from xscore.shared.exam_paths import (
    artifact_scaffold_markdown_path,
    exam_artifact_dir,
)
from xscore.shared.models import (
    ExamLayout,
    ExamScaffold,
    Question,
    gradable_questions,
)
from xscore.scaffold.pdf_parser.content import (
    default_mcq_leaf_marks,
    rollup_question_marks,
)
from xscore.scaffold.scaffold_cache import (
    _cache_path_under_exam_folder,
    _clear_legacy_scaffold_outputs,
    _compute_pdf_sha256,
    _effective_cache_path,
    _load_cache,
    _migrate_scaffold_cache_to_artifact,
    _read_cached_source_hashes,
    _save_cache,
    _scaffold_to_payload,
)
from xscore.scaffold.scaffold_markdown import write_scaffold_markdown


def find_exam_pdf(folder: Path) -> Path:
    """Pick the vector exam PDF for parsing.

    Prefers files with 'empty', 'exam', or Cambridge '_qp_' in the name; skips
    scan/answer/student files and Cambridge '_ms_' mark schemes.
    """
    _SKIP = ("scan", "answer", "student", "_ms_")
    pdfs = [f for f in folder.glob("*.pdf") if not any(kw in f.name.lower() for kw in _SKIP)]
    if not pdfs:
        raise FileNotFoundError(f"No exam PDF found in {folder}")
    preferred = [f for f in pdfs if any(kw in f.name.lower() for kw in ("empty", "exam", "_qp_"))]
    return preferred[0] if preferred else pdfs[0]


def find_answer_pdf(folder: Path) -> Path | None:
    answer_pdfs = [
        f for f in folder.glob("*.pdf")
        if "answer" in f.name.lower() or "_ms_" in f.name.lower()
    ]
    return answer_pdfs[0] if answer_pdfs else None


# Backwards-compat alias for any caller still on the old name.
_find_answer_pdf = find_answer_pdf


def _source_pdfs(folder: Path, exam_pdf_override: Path | None = None) -> list[Path]:
    """Return only the PDFs that the scaffold is built from (exam + answer key)."""
    try:
        exam = exam_pdf_override or find_exam_pdf(folder)
        sources = [exam]
    except FileNotFoundError:
        sources = []
    ans = find_answer_pdf(folder)
    if ans is not None:
        sources.append(ans)
    return sources


def _is_cache_valid(
    folder: Path, artifact_dir: Path, exam_pdf_override: Path | None = None
) -> bool:
    cache = _effective_cache_path(folder, artifact_dir)
    if cache is None:
        return False
    sources = _source_pdfs(folder, exam_pdf_override)
    if not sources:
        return False

    # Prefer content-hash validation when the cache stores hashes. mtime is
    # fragile: ``cp -p`` preserves mtime across content changes, ``touch``
    # bumps mtime without changing content, and ``--resume-dir`` against a
    # folder whose empty exam was edited would silently load a stale
    # scaffold under the old mtime check. New caches always include hashes;
    # legacy XML/JSON caches without them fall through to mtime.
    cached_hashes = _read_cached_source_hashes(cache)
    if cached_hashes:
        for pdf in sources:
            stored = cached_hashes.get(pdf.name)
            if stored is None:
                return False
            current = _compute_pdf_sha256(pdf)
            if not current or current != stored:
                return False
        return True

    cache_mtime = cache.stat().st_mtime
    for pdf in sources:
        if pdf.stat().st_mtime > cache_mtime:
            return False
    return True


def build_scaffold(
    folder: Path,
    client: Any | None = None,
    dpi: int = 200,
    *,
    artifact_dir: Path | None = None,
    output_base: str | Path = "output",
    quiet: bool = False,
    exam_pdf_override: Path | None = None,
    on_exam_complete: "Any | None" = None,
    on_graphics_complete: "Any | None" = None,
    on_scheme_complete: "Any | None" = None,
    on_layout_complete: "Any | None" = None,
    on_cut_complete: "Any | None" = None,
    students: "list[str] | None" = None,
    force_rebuild: bool = False,
) -> ExamScaffold:
    """Build (or load from cache) the ExamScaffold for the exam in *folder*.

    Derived files (cache, ``scaffold_images``, overlay PDF) go under *artifact_dir*
    (default: ``output/<exam_stem>/``). *client* is optional and unused.
    *dpi* is unused; parsing is vector-based.
    *quiet*: when True, omit cache-hit log lines.
    *exam_pdf_override*: use this PDF instead of auto-detecting one in *folder*.
    *force_rebuild*: when True, skip cache entirely and always re-run AI extraction.
    """
    _ = client, dpi
    from xscore.shared.terminal_ui import tool_line

    ad = artifact_dir or exam_artifact_dir(folder, output_base)

    if not force_rebuild and _is_cache_valid(folder, ad, exam_pdf_override):
        try:
            if not quiet:
                tool_line("scaffold", "Loading scaffold from cache …")
            loaded_path = _effective_cache_path(folder, ad)
            scaffold = _load_cache(folder, ad)
            if loaded_path is not None and _cache_path_under_exam_folder(
                loaded_path, folder
            ):
                if not quiet:
                    tool_line(
                        "scaffold",
                        "Migrating scaffold cache and images from exam folder → output …",
                    )
                _migrate_scaffold_cache_to_artifact(folder, ad, scaffold)
            elif not artifact_scaffold_markdown_path(ad).is_file():
                write_scaffold_markdown(ad, _scaffold_to_payload(scaffold, []))
            return scaffold
        except (ValueError, KeyError, TypeError, json.JSONDecodeError, ET.ParseError):
            tool_line("scaffold", "Cache incompatible or corrupt — rebuilding …")

    exam_pdf = exam_pdf_override or find_exam_pdf(folder)

    from xscore.scaffold.ai_scaffold import build_ai_scaffold
    ans = find_answer_pdf(folder)
    questions, layout = build_ai_scaffold(
        exam_pdf, ans,
        on_layout_complete=on_layout_complete,
        on_cut_complete=on_cut_complete,
        on_exam_complete=on_exam_complete,
        on_graphics_complete=on_graphics_complete,
        on_scheme_complete=on_scheme_complete,
        artifact_dir=ad,
    )
    return finalize_scaffold(folder, exam_pdf, questions, layout, students=students, artifact_dir=ad)


def finalize_scaffold(
    folder: Path,
    exam_pdf: Path,
    questions: list[Question],
    layout: ExamLayout,
    *,
    students: "list[str] | None" = None,
    artifact_dir: Path | None = None,
) -> ExamScaffold:
    """Step extract_exam_questions finishing work: mark rollups → page count → ``ExamScaffold`` → cache.

    Splits the post-merge work out of :func:`build_scaffold` so XScore.py can
    call the six scaffold step functions directly and still produce the cached
    ``ExamScaffold`` artifact.
    """
    if not questions:
        raise RuntimeError(
            "No questions extracted from exam PDF. "
            "Check GOOGLE_API_KEY and that the PDF is readable."
        )

    for q in questions:
        default_mcq_leaf_marks(q)  # MCQ leaves: marks 0 → 1
        rollup_question_marks(q)   # parents: marks = sum of children

    import fitz

    doc = fitz.open(exam_pdf)
    try:
        page_count = len(doc)
    finally:
        doc.close()

    leaves = gradable_questions(questions)
    total_marks = sum(q.marks for q in leaves)
    raw_description = (
        f"{len(questions)} top-level, {len(leaves)} gradable parts, {total_marks} marks; "
        + ", ".join(f"Q{q.number}({q.marks}m)" for q in leaves[:24])
        + (" …" if len(leaves) > 24 else "")
    )

    scaffold = ExamScaffold(
        questions=questions,
        total_marks=total_marks,
        page_count=page_count,
        raw_description=raw_description,
        layout=layout,
    )
    ad = artifact_dir or exam_artifact_dir(folder)
    # Snapshot SHA-256 of every source PDF so the next run's cache-validity
    # check can detect a content change even when mtime hasn't moved.
    source_hashes = {
        p.name: _compute_pdf_sha256(p) for p in _source_pdfs(folder, exam_pdf)
    }
    _save_cache(ad, scaffold, students, source_hashes=source_hashes)
    _clear_legacy_scaffold_outputs(folder)
    return scaffold
