"""Per-paper enrichment + per-question vector PDF snippet for the eXam pipeline.

Two entry points:

- ``ensure_question_pdf(paper, qnum, subject)`` — render one question.pdf via the
  existing eXercise primitives (`find_question_positions` → `get_question_regions`
  → `collect_vector_strips` → `layout_vector_strips_to_pdf`). Cached on disk.

- ``ensure_paper_indexed(paper, ms, subject)`` — run the xscore scaffold + scheme
  *phase functions* (no _Ctx) to produce structured ``exam_questions.yaml`` and
  ``mark_scheme.yaml``, then render snippets for every top-level question.

Cache layout: ``output/eXam/bank/<subject>/<paper_stem>/``.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import fitz
import yaml

from eXercise.config import PROJECT_ROOT, get_subject_config
from eXercise.questions import find_question_positions, get_question_regions
from eXercise.rendering import collect_vector_strips, layout_vector_strips_to_pdf

BANK_ROOT = PROJECT_ROOT / "output" / "eXam" / "bank"

CS_SUBJECTS = {"igcse_computer_science", "a_level_computer_science"}


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:16]


def bank_dir_for(subject: str, paper_path: Path) -> Path:
    return BANK_ROOT / subject / paper_path.stem


def ensure_question_pdf(
    paper_path: Path,
    qnum: int,
    subject: str | None = None,
    *,
    cfg=None,
) -> Path:
    """Render question.pdf for one (paper, qnum). Cached on disk."""
    paper_path = Path(paper_path)
    out_dir = bank_dir_for(subject or "unknown", paper_path) / str(qnum)
    out_path = out_dir / "question.pdf"
    if out_path.exists():
        return out_path
    out_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg or get_subject_config(subject)
    doc = fitz.open(paper_path)
    try:
        positions = find_question_positions(doc, cfg)
        regions = get_question_regions(doc, positions, [qnum], cfg)
        if not regions:
            raise RuntimeError(
                f"Question {qnum} not found in {paper_path.name}"
            )
        strips = collect_vector_strips(doc, regions, is_ms=False, cfg=cfg)
        layout_vector_strips_to_pdf(
            strips, str(out_path), header_label=None, name_field=False,
            trim_to_content=True,
        )
    finally:
        doc.close()
    return out_path


def ensure_paper_indexed(
    paper_path: Path,
    ms_path: Path | None,
    subject: str,
) -> Path:
    """Run xscore phase functions + render snippets. Returns the bank dir.

    Idempotent: a (paper_sha, ms_sha) check at ``paper_sha.txt`` short-circuits
    re-runs unless the source files change.
    """
    paper_path = Path(paper_path)
    ms_path = Path(ms_path) if ms_path else None
    out_dir = bank_dir_for(subject, paper_path)
    out_dir.mkdir(parents=True, exist_ok=True)

    sha_file = out_dir / "paper_sha.txt"
    paper_sha = _file_sha(paper_path)
    ms_sha = _file_sha(ms_path) if ms_path else ""
    cur = f"{paper_sha}\n{ms_sha}\n"
    if (
        sha_file.exists()
        and sha_file.read_text(encoding="utf-8") == cur
        and (out_dir / "exam_questions.yaml").exists()
    ):
        print(f"[bank] {paper_path.name}: cache hit, skipping enrichment")
        return out_dir

    # Lazy imports — xscore pulls heavy deps (google.genai, …) at import time.
    from eXercise.ai_client import make_gemini_native_client
    from eXercise.env_load import load_project_env
    load_project_env()
    from eXam.xscore_adapter import load_scaffold_api
    xs = load_scaffold_api()

    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) not set — required for xscore "
            "scaffold extraction. Set it in .env."
        )
    fmt = xs.get_scaffold_format()
    is_cs = subject in CS_SUBJECTS

    # ── QP chain ─────────────────────────────────────────────────────────────
    layout_result, layout_elapsed, layout_model = xs.detect_layout_phase(
        client, paper_path, out_dir,
    )
    actual_pdf, split_temp, _n_phys, n_split = xs.cut_exam_pdf_phase(
        paper_path, layout_result, out_dir,
        layout_model=layout_model, layout_elapsed=layout_elapsed,
    )
    try:
        detect_model, detect_thinking, detect_max = xs.extract_question_numbers_model_config()
        scaffold_nodes, _raw_layout = xs.extract_exam_question_numbers(
            client, detect_model, detect_thinking, detect_max,
            actual_exam_pdf=actual_pdf,
            layout_result=layout_result,
            split_pdf_path=split_temp,
            n_split_pages=n_split,
            artifact_dir=out_dir,
            fmt=fmt, is_cs=is_cs, should_cache=False,
        )
        fill_model, fill_thinking, fill_max = xs.extract_questions_model_config()
        raw_questions = xs.extract_exam_questions(
            client, fill_model, fill_thinking, fill_max,
            actual_exam_pdf=actual_pdf,
            scaffold_nodes=scaffold_nodes,
            artifact_dir=out_dir,
            fmt=fmt, is_cs=is_cs, should_cache=False,
        )
    finally:
        if split_temp:
            try:
                split_temp.unlink(missing_ok=True)
            except OSError:
                pass

    (out_dir / "exam_questions.yaml").write_text(
        yaml.safe_dump({"questions": raw_questions}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # ── MS chain ─────────────────────────────────────────────────────────────
    if ms_path is not None:
        graphics_by_qnum, _ = xs.detect_scheme_graphics_phase(
            ms_path, raw_questions, out_dir, fmt=fmt,
        )
        questions_per_page = xs.assign_scheme_questions_phase(
            client, ms_path, raw_questions, out_dir,
        )
        scheme_data = xs.parse_mark_scheme_phase(
            client, ms_path, raw_questions, graphics_by_qnum, questions_per_page,
            out_dir, fmt=fmt, is_cs=is_cs,
        )
        (out_dir / "mark_scheme.yaml").write_text(
            yaml.safe_dump(scheme_data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    # ── Render question.pdf for each top-level question ──────────────────────
    cfg = get_subject_config(subject)
    rendered = 0
    skipped = 0
    for q in raw_questions:
        qnum_raw = q.get("number") if isinstance(q, dict) else None
        try:
            qnum_int = int(qnum_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue
        try:
            ensure_question_pdf(paper_path, qnum_int, subject=subject, cfg=cfg)
            rendered += 1
        except Exception as e:  # noqa: BLE001
            print(f"[bank] snippet render failed q{qnum_int}: {e}")
            skipped += 1

    sha_file.write_text(cur, encoding="utf-8")
    print(
        f"[bank] {paper_path.name}: indexed "
        f"({rendered} snippets, {skipped} skipped) → {out_dir}"
    )
    return out_dir


def finalize_authored_paper(
    paper_path: Path,
    ms_path: Path | None,
    subject: str,
) -> Path:
    """Finalize a paper whose ``exam_questions.yaml`` / ``mark_scheme.yaml`` were
    authored by hand (no AI extraction): render the per-question snippet PDFs and
    stamp ``paper_sha.txt`` so a later ``ensure_paper_indexed`` cache-hits instead
    of re-running the (paid) scaffold chain.

    The ``(paper_sha, ms_sha)`` stamp must match what ``ensure_paper_indexed``
    would compute, so pass the same *ms_path* ``open_mode.pair_mark_scheme`` finds
    next to the QP — otherwise the lazy-index path would see a mismatch and re-run
    the AI chain, overwriting the authored YAMLs.
    """
    paper_path = Path(paper_path)
    ms_path = Path(ms_path) if ms_path else None
    out_dir = bank_dir_for(subject, paper_path)
    qy = out_dir / "exam_questions.yaml"
    if not qy.exists():
        raise RuntimeError(
            f"{qy} not found — author exam_questions.yaml before --authored finalize."
        )
    raw_questions = (yaml.safe_load(qy.read_text(encoding="utf-8")) or {}).get(
        "questions"
    ) or []

    cfg = get_subject_config(subject)
    rendered = 0
    skipped = 0
    for q in raw_questions:
        qnum_raw = q.get("number") if isinstance(q, dict) else None
        try:
            qnum_int = int(qnum_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue
        try:
            ensure_question_pdf(paper_path, qnum_int, subject=subject, cfg=cfg)
            rendered += 1
        except Exception as e:  # noqa: BLE001
            print(f"[bank] snippet render failed q{qnum_int}: {e}")
            skipped += 1

    ms_sha = _file_sha(ms_path) if ms_path else ""
    (out_dir / "paper_sha.txt").write_text(
        f"{_file_sha(paper_path)}\n{ms_sha}\n", encoding="utf-8"
    )
    print(
        f"[bank] {paper_path.name}: finalized authored "
        f"({rendered} snippets, {skipped} skipped) → {out_dir}"
    )
    return out_dir


def _cli() -> int:
    p = argparse.ArgumentParser(prog="eXam.bank")
    p.add_argument("--paper", required=True, help="question-paper PDF")
    p.add_argument(
        "--ms",
        default=None,
        help="mark-scheme PDF (optional but required for non-MCQ marking)",
    )
    p.add_argument(
        "--subject",
        required=True,
        help="subject slug, e.g. igcse_physics, a_level_computer_science",
    )
    p.add_argument(
        "--question",
        type=int,
        default=None,
        help="if set, only render the snippet for this question (no xscore enrichment)",
    )
    p.add_argument(
        "--authored",
        action="store_true",
        help="skip AI extraction; render snippets + stamp the cache from "
        "hand-authored exam_questions.yaml / mark_scheme.yaml already in the bank dir",
    )
    args = p.parse_args()
    paper = Path(args.paper).resolve()
    if not paper.exists():
        print(f"error: paper not found: {paper}", file=sys.stderr)
        return 2
    ms = Path(args.ms).resolve() if args.ms else None
    if ms is not None and not ms.exists():
        print(f"error: mark scheme not found: {ms}", file=sys.stderr)
        return 2
    if args.question is not None:
        out = ensure_question_pdf(paper, args.question, subject=args.subject)
        print(f"snippet: {out}")
        return 0
    if args.authored:
        out = finalize_authored_paper(paper, ms, args.subject)
        print(f"bank dir: {out}")
        return 0
    out = ensure_paper_indexed(paper, ms, args.subject)
    print(f"bank dir: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
