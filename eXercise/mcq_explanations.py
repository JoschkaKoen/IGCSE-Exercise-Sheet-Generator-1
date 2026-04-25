# -*- coding: utf-8 -*-
"""AI-generated explanations for MCQ answers: text extraction → LLM → LaTeX → pdflatex → VectorStrips.

The public entry point is ``generate_mcq_explanation_strips``. It returns a list of
``VectorStrip`` objects (one per LaTeX output page) that slot directly into the
``layout_vector_strips_to_pdf`` pipeline, or ``[]`` on any failure so callers can
fall back to ``create_mcq_answer_strips``.

This module is an orchestration façade. The implementation is split across:
- ``mcq_image_extract``  — extract question text and images from QP PDFs
- ``mcq_ai``             — build prompts and call the LLM
- ``mcq_latex``          — assemble the LaTeX document source
- ``mcq_compile``        — run pdflatex and convert pages to VectorStrips
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import fitz

from .config import SubjectConfig

if TYPE_CHECKING:
    from .rendering import VectorStrip

try:
    from .ai_client import format_model_announcement, make_ai_client
    _AI_CLIENT_AVAILABLE = True
except ImportError:
    format_model_announcement = None  # type: ignore[assignment]
    make_ai_client = None  # type: ignore[assignment]
    _AI_CLIENT_AVAILABLE = False

from .env_load import load_project_env

# ---------------------------------------------------------------------------
# Sub-module re-exports (keep public API stable for pipeline.py and tests)
# ---------------------------------------------------------------------------

from .mcq_image_extract import (  # noqa: F401
    extract_mcq_question_texts,
    mcq_questions_with_images,
    rasterize_mcq_images,
)
from .mcq_ai import generate_mcq_explanations  # noqa: F401
from .mcq_latex import build_explanation_latex  # noqa: F401
from .mcq_compile import compile_latex, _pdf_to_vector_strips  # noqa: F401


# ---------------------------------------------------------------------------
# Batched AI workflow (prepare → one combined call → finalize per paper)
# ---------------------------------------------------------------------------


@dataclass
class McqPaperData:
    """All data needed to generate AI explanations for one MCQ paper, with no API call made yet."""

    qs: list[int]
    answers: dict[int, str]
    answered: list[int]
    q_texts: dict[int, str]
    q_images: dict[int, str]  # {qnum: base64_png} for questions with diagrams/figures
    q_pdf_bytes: bytes | None  # PDF of question strips for Gemini native upload; None for non-Gemini
    exam_key: str | None
    paper_label: str
    expl_pdf_path: Path


def _build_mcq_questions_pdf(
    qp_doc: fitz.Document,
    regions: list[tuple[int, int, float, float]],
    answered: list[int],
    cfg: SubjectConfig,
    paper_label: str,
) -> bytes:
    """Build a PDF of the MCQ question strips using the existing layout pipeline.

    Reuses ``collect_vector_strips`` + ``layout_vector_strips_to_pdf`` — the same
    path that renders questions into the exercise sheet — so the output is
    guaranteed to look correct. Includes the paper-number header; no name field.
    Returns the PDF as bytes (a temp file is created and deleted internally).
    """
    from .output_paths import temp_pdf_path  # noqa: PLC0415
    from .rendering import collect_vector_strips, layout_vector_strips_to_pdf  # noqa: PLC0415

    answered_set = set(answered)
    answered_regions = [r for r in regions if r[0] in answered_set]
    strips = collect_vector_strips(qp_doc, answered_regions, is_ms=False, cfg=cfg)

    with temp_pdf_path() as tmp_path:
        layout_vector_strips_to_pdf(
            strips,
            str(tmp_path),
            header_label=paper_label,
            name_field=False,
            page_number_circle=False,
        )
        return tmp_path.read_bytes()


def prepare_mcq_job_data(
    qp_doc: fitz.Document,
    regions: list[tuple[int, int, float, float]],
    answers: dict[int, str],
    qs: list[int],
    cfg: SubjectConfig,
    exam_key: str | None,
    paper_label: str,
    expl_pdf_path: Path,
) -> McqPaperData | None:
    """Extract question texts and assemble :class:`McqPaperData`. No API call is made.

    Returns ``None`` if there are no answered questions to process.
    """
    from .mcq_image_extract import (  # noqa: PLC0415
        extract_mcq_question_texts,
        mcq_questions_with_images,
        rasterize_mcq_images,
    )

    answered = [q for q in qs if q in answers]
    if not answered:
        return None

    # Detect provider to choose between Gemini PDF-upload path and OpenAI-compat path.
    from .ai_client import resolve_active_model  # noqa: PLC0415
    _, _provider, _ = resolve_active_model(
        ("MCQ_MODEL", "AI_MCQ_MODEL", "AI_DEFAULT_MODEL"),
    )

    q_texts = extract_mcq_question_texts(qp_doc, regions, qs, cfg)
    missing = [q for q in answered if q not in q_texts]
    if missing:
        print(f"  MCQ explanations: no text extracted for Q{missing} (will use placeholder).")

    # Image rasterization — only needed for the non-Gemini (OpenAI-compat) path.
    q_images: dict[int, str] = {}
    if _provider != "gemini":
        img_qs = mcq_questions_with_images(qp_doc, regions, answered, cfg)
        if img_qs:
            print(f"  MCQ questions with images: Q{sorted(img_qs)} — rasterizing for vision…")
            debug_dir = expl_pdf_path.parent / "mcq_images"
            q_images = rasterize_mcq_images(qp_doc, regions, img_qs, cfg, debug_dir=debug_dir)
            print(f"  Rasterized {len(q_images)} question image(s) → {debug_dir}")

    # Build questions PDF for the Gemini native-upload path.
    q_pdf_bytes: bytes | None = None
    if _provider == "gemini":
        print(f"  Building MCQ questions PDF for Gemini upload ({len(answered)} questions)…")
        q_pdf_bytes = _build_mcq_questions_pdf(qp_doc, regions, answered, cfg, paper_label)
        # Derive a unique per-paper filename from expl_pdf_path (which already
        # contains the paper index) to avoid overwriting when papers run in parallel.
        mcq_q_pdf_path = expl_pdf_path.with_name(expl_pdf_path.stem + "_questions.pdf")
        mcq_q_pdf_path.write_bytes(q_pdf_bytes)
        print(f"  MCQ questions PDF: {len(q_pdf_bytes):,} bytes → {mcq_q_pdf_path}")

    return McqPaperData(
        qs=qs,
        answers=answers,
        answered=answered,
        q_texts=q_texts,
        q_images=q_images,
        q_pdf_bytes=q_pdf_bytes,
        exam_key=exam_key,
        paper_label=paper_label,
        expl_pdf_path=expl_pdf_path,
    )


def batch_generate_mcq_explanations(
    papers: list[McqPaperData],
    stream_thinking: bool = True,
) -> list[dict[int, list[str]]]:
    """Fire one focused AI call **per paper** in parallel threads.

    Each paper uses the same proven single-paper prompt that worked before,
    so the model always gives a reliable per-paper response.  The parallelism
    means N papers take roughly the same wall-clock time as a single paper.

    Returns one explanations dict per paper (in the same order as *papers*).
    Falls back to ``{}`` for any paper whose call fails, so the caller can
    render the plain-answer fallback strip via :func:`create_mcq_answer_strips`.
    """
    from .mcq_ai import generate_mcq_explanations  # noqa: PLC0415

    if not papers:
        return []
    client_model = _load_ai_client()
    if client_model is None:
        return [{} for _ in papers]
    client, model, provider, thinking_tokens, max_tokens = client_model

    total_qs = sum(len(p.answered) for p in papers)
    print(
        f"  Calling AI for explanations "
        f"({len(papers)} paper(s), {total_qs} question(s) total, parallel)…"
    )

    def _call_one(paper: McqPaperData) -> dict[int, list[str]]:
        # Use a per-paper debug subdirectory so parallel calls don't overwrite
        # each other's debug files (prompt, response, thinking).
        _debug_dir = paper.expl_pdf_path.parent / paper.expl_pdf_path.stem
        return generate_mcq_explanations(
            client, model,
            paper.q_texts, paper.answers, paper.answered, paper.exam_key,
            q_images=paper.q_images,
            provider=provider,
            thinking_tokens=thinking_tokens,
            max_tokens=max_tokens,
            save_dir=_debug_dir,
            q_pdf_bytes=paper.q_pdf_bytes,
            stream_thinking=stream_thinking,
        )

    # Cap concurrency: one connection per paper hits provider rate limits at scale.
    # Override with MCQ_BATCH_CONCURRENCY env var if you know the active key tier
    # supports more.
    try:
        cap = max(1, int(os.environ.get("MCQ_BATCH_CONCURRENCY", "4")))
    except ValueError:
        cap = 4
    workers = min(len(papers), cap)
    results: list[dict[int, list[str]]] = [{} for _ in papers]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_idx = {pool.submit(_call_one, p): i for i, p in enumerate(papers)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                print(f"  MCQ explanations: paper {idx} failed: {exc}")
    return results


def finalize_mcq_explanation_strips(
    job_data: McqPaperData,
    explanations: dict[int, list[str]],
) -> list[Any]:
    """Build LaTeX, compile, and convert to VectorStrips from pre-computed *explanations*.

    Returns ``[]`` if explanations are empty or compilation fails, so the caller can
    fall back to :func:`create_mcq_answer_strips`.
    """
    from .mcq_compile import compile_latex, _pdf_to_vector_strips  # noqa: PLC0415
    from .mcq_latex import build_explanation_latex  # noqa: PLC0415

    if not explanations:
        return []

    n_expl = len(explanations)
    print(f"  Received explanations for {n_expl}/{len(job_data.answered)} question(s).")

    tex = build_explanation_latex(job_data.qs, job_data.answers, explanations, job_data.paper_label, job_data.exam_key)

    if os.environ.get("SAVE_TEX", "").lower() in ("true", "1", "yes"):
        tex_file_path = job_data.expl_pdf_path.with_suffix(".tex")
        tex_file_path.write_text(tex, encoding="utf-8")
        print(f"  Saved TeX: {tex_file_path}")

    print("  Compiling LaTeX…")
    success = compile_latex(tex, job_data.expl_pdf_path)
    if not success:
        return []

    first_q = job_data.answered[0] if job_data.answered else None
    try:
        strips = _pdf_to_vector_strips(job_data.expl_pdf_path, job_data.answered, first_q)
    finally:
        job_data.expl_pdf_path.unlink(missing_ok=True)

    print(f"  MCQ explanation: {len(strips)} page(s) of explanations added.")
    return strips


def _load_ai_client() -> tuple[Any, str, str, str | None] | None:
    """Load LLM client from environment; return 5-tuple from make_ai_client or None."""
    if not _AI_CLIENT_AVAILABLE or make_ai_client is None:
        print("  MCQ explanations: ai_client module unavailable.")
        return None

    load_project_env()

    # Resolution order: MCQ_MODEL → AI_MCQ_MODEL → AI_DEFAULT_MODEL → default.
    # Provider and thinking effort are inferred from the resolved model string.
    result = make_ai_client(model_env="MCQ_MODEL", legacy_model_env="AI_MCQ_MODEL")
    if result is None:
        # API key missing for that model's provider; try the global AI_DEFAULT_MODEL.
        result = make_ai_client(model_env="AI_DEFAULT_MODEL", legacy_model_env="XAI_MODEL")
    if result is None:
        print("  MCQ explanations: no API key set for active model; skipping AI explanations.")
        return None
    _client, _model, _provider, _thinking, _max_tokens = result
    eff_max = _max_tokens if _max_tokens is not None else 16384
    print(f"  {format_model_announcement(_model, _thinking, eff_max)}")
    return result


def generate_mcq_explanation_strips(
    qp_doc: fitz.Document,
    regions: list[tuple[int, int, float, float]],
    answers: dict[int, str],
    qs: list[int],
    cfg: SubjectConfig,
    exam_key: str | None,
    paper_label: str,
    expl_pdf_path: Path,
) -> list[Any]:
    """Generate AI explanation strips for the given MCQ job.

    Returns a list of ``VectorStrip`` objects (one per LaTeX output page) on
    success, or ``[]`` on any failure.

    Parameters
    ----------
    qp_doc:
        Open PyMuPDF document for the question paper.
    regions:
        ``(qnum, page_idx, y_start, y_end)`` tuples from ``get_question_regions``.
    answers:
        ``{qnum: letter}`` from ``parse_mcq_answers``.
    qs:
        The requested question numbers for this job.
    cfg:
        Subject config (for clip rect constants).
    exam_key:
        ``"physics"``, ``"computer_science"``, ``"mathematics"``, or ``None``.
    paper_label:
        Human-readable label for the paper (used in the LaTeX title).
    expl_pdf_path:
        Where to write the compiled PDF (inside the run's output dir).
    """
    print("  Generating AI explanations for MCQ answers…")

    job_data = prepare_mcq_job_data(
        qp_doc=qp_doc,
        regions=regions,
        answers=answers,
        qs=qs,
        cfg=cfg,
        exam_key=exam_key,
        paper_label=paper_label,
        expl_pdf_path=expl_pdf_path,
    )
    if job_data is None:
        return []

    [explanations] = batch_generate_mcq_explanations([job_data])
    return finalize_mcq_explanation_strips(job_data, explanations)
