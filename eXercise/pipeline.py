# -*- coding: utf-8 -*-
"""Orchestrate extraction jobs and merge mark scheme output."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import fitz

from .config import PAGE_HEADER_BY_EXAM, get_subject_config
from .exceptions import ExtractionError
from .labels import page_header_label, paper_label_from_qp_path
from .mark_scheme import detect_ms_type, find_ms_answer_regions, parse_mcq_answers
from .mcq_explanations import (
    McqPaperData,
    batch_generate_mcq_explanations,
    finalize_mcq_explanation_strips,
    generate_mcq_explanation_strips,
    prepare_mcq_job_data,
)
from .difficulty_ranking import generate_difficulty_ranking
from .pdfjam_post import run_exercise_sheet_pdfjam_variants
from .questions import find_question_positions, get_question_regions
from .rendering import (
    GapStrip,
    Strip,
    VectorStrip,
    collect_vector_strips,
    create_mcq_answer_strips,
    layout_vector_strips_to_pdf,
)


def build_exercise_overview(anchors: list[dict[str, Any]]) -> dict[str, Any]:
    """Group flat anchors into papers for the web UI; each exercise has page + y_pt."""
    papers: list[dict[str, Any]] = []
    for a in anchors:
        label = (a.get("paper") or "") or ""
        if not papers or papers[-1]["label"] != label:
            papers.append({"label": label, "exercises": []})
        ex_entry: dict[str, Any] = {
            "q": int(a["q"]),
            "page": int(a["page"]),
            "y_pt": float(a["y_pt"]),
        }
        if "y_view_pt" in a:
            ex_entry["y_view_pt"] = float(a["y_view_pt"])
        papers[-1]["exercises"].append(ex_entry)
    return {"papers": papers, "anchors": anchors}


def merge_mcq_flags_into_overview(
    overview: dict[str, Any],
    jobs: list[dict],
    job_mcq_ms: list[bool],
    *,
    use_paper_sublabels: bool,
) -> None:
    """Set ``mcq: true`` on exercises whose job uses an MCQ-style mark scheme.

    Keys must match how ``layout_vector_strips_to_pdf`` records ``paper`` on anchors:
    with exam sublabels that is the short paper label; otherwise anchors use ``None``,
    which ``build_exercise_overview`` turns into ``\"\"``.
    """
    mcq_pairs: set[tuple[str, int]] = set()
    for j_idx, job in enumerate(jobs):
        if j_idx >= len(job_mcq_ms) or not job_mcq_ms[j_idx]:
            continue
        plab = paper_label_from_qp_path(job["input_pdf"])
        for q in job.get("questions") or []:
            qn = int(q)
            if use_paper_sublabels:
                mcq_pairs.add((plab, qn))
            else:
                mcq_pairs.add(("", qn))
    for paper in overview.get("papers") or []:
        plab = paper.get("label")
        pkey = "" if plab is None else str(plab)
        for ex in paper.get("exercises") or []:
            ex["mcq"] = (pkey, int(ex["q"])) in mcq_pairs


def merge_answer_anchors_into_overview(
    overview: dict[str, Any], answer_anchors: list[dict[str, Any]]
) -> None:
    """Add ``answers_page`` / ``answers_y_pt`` to each exercise when MS layout has anchors."""
    key_to_pos: dict[tuple[str, int], tuple[int, float, float]] = {}
    for a in answer_anchors:
        plab = a.get("paper")
        paper_key = "" if plab is None else str(plab)
        ypt = float(a["y_pt"])
        yv = float(a.get("y_view_pt", ypt))
        key_to_pos[(paper_key, int(a["q"]))] = (int(a["page"]), ypt, yv)
    for paper in overview.get("papers") or []:
        plab = paper.get("label")
        pkey = "" if plab is None else str(plab)
        for ex in paper.get("exercises") or []:
            pos = key_to_pos.get((pkey, int(ex["q"])))
            if pos:
                ex["answers_page"] = pos[0]
                ex["answers_y_pt"] = pos[1]
                ex["answers_y_view_pt"] = pos[2]


def merge_pdf_files(part_paths: list[str], dest: str) -> None:
    """Concatenate multiple PDF files into a single output PDF."""
    merged = fitz.open()
    for p in part_paths:
        src = fitz.open(p)
        merged.insert_pdf(src)
        src.close()
    merged.save(dest, deflate=True, garbage=4)
    merged.close()


def run_extraction_jobs(
    jobs: list[dict], output_pdf: str, exam_key: str | None = None
) -> dict[str, Any]:
    """
    Each job dict: ``input_pdf``, ``questions``, ``mark_scheme_pdf`` (optional path).
    All question strips are concatenated and laid out in one vector PDF flow.
    Source documents are kept open until layout is complete, then closed.

    Returns an overview dict for the web UI (see ``build_exercise_overview``): exercise
    ``page`` / ``y_pt`` per question, plus optional ``answers_page`` / ``answers_y_pt``
    when a structured mark-scheme layout produced matching anchors.
    """
    if not jobs:
        raise ExtractionError("No extraction jobs.")

    exercise_anchors: list[dict[str, Any]] = []
    answer_anchors: list[dict[str, Any]] | None = None

    cfg = get_subject_config(exam_key)
    page_header = page_header_label(jobs, exam_key)
    use_paper_sublabels = exam_key is not None and exam_key in PAGE_HEADER_BY_EXAM

    qp_docs: list[fitz.Document] = []
    ms_docs: list[fitz.Document] = []

    # Regions per job (parallel to jobs list); always appended, even when empty,
    # so qp_docs[i], job_regions[i], and jobs[i] are always in sync.
    job_regions: list[list[tuple[int, int, float, float]]] = []

    all_strips: list[Strip] = []
    all_ms_strips: list[Strip] = []
    job_mcq_ms: list[bool] = [False] * len(jobs)

    try:
        for job in jobs:
            ip = job["input_pdf"]
            qs = job["questions"]
            paper_lbl = paper_label_from_qp_path(ip)
            print(f"\nQuestion paper: {ip}")
            doc = fitz.open(ip)
            qp_docs.append(doc)
            print(f"  PDF has {len(doc)} pages")
            positions = find_question_positions(doc, cfg)
            found_nums = sorted(set(p[0] for p in positions))
            print(f"  Found questions: {found_nums}")
            if qs == "all":
                qs = found_nums
                job["questions"] = qs
            print(f"  Questions: {qs}")
            regions = get_question_regions(doc, positions, qs, cfg)
            job_regions.append(regions)  # always append before any continue
            if not regions:
                print("  Warning: No matching questions for this paper, skipping.")
                continue
            print(f"  Extracting {len(regions)} region(s) for questions {sorted(set(r[0] for r in regions))}")
            strips = collect_vector_strips(doc, regions, cfg=cfg)
            if not strips:
                continue
            if use_paper_sublabels:
                if all_strips:
                    all_strips.append(GapStrip(height_pt=4.0))
                all_strips.append(paper_lbl)
            elif len(jobs) > 1 and all_strips:
                all_strips.append(GapStrip(height_pt=4.0))
            all_strips.extend(strips)

        if not all_strips:
            raise ExtractionError("No matching questions found in any paper.")

        print(f"\nOutput: {output_pdf}")
        exercise_anchors = layout_vector_strips_to_pdf(all_strips, output_pdf, page_header, name_field=True)

        out_path = Path(output_pdf)
        answers_path = out_path.parent / f"{out_path.stem}_answers{out_path.suffix}"

        # ── Phase 1: open all mark-scheme docs, prepare MCQ data (no AI calls) ──
        # ms_info[j_idx] is None when a job has no mark scheme, otherwise a dict.
        ms_info: list[dict | None] = []
        mcq_prepared: dict[int, McqPaperData] = {}

        for j_idx, job in enumerate(jobs):
            ms = job.get("mark_scheme_pdf")
            if not ms:
                ms_info.append(None)
                continue
            print(f"\nMark scheme: {ms}")
            ms_doc = fitz.open(ms)
            ms_docs.append(ms_doc)
            ms_type = detect_ms_type(ms_doc)
            job_mcq_ms[j_idx] = ms_type == "mcq"
            print(f"  Type: {ms_type}, {len(ms_doc)} pages")
            qs = job["questions"]
            paper_lbl = paper_label_from_qp_path(job["input_pdf"])
            info: dict = {"doc": ms_doc, "type": ms_type, "qs": qs, "label": paper_lbl}

            if ms_type == "mcq":
                answers = parse_mcq_answers(ms_doc)
                info["answers"] = answers
                found_ans = [q for q in qs if q in answers]
                print(f"  Found answers for: {found_ans}")
                expl_pdf_path = out_path.parent / f"{out_path.stem}_mcq_expl_{j_idx}.pdf"
                prepared = prepare_mcq_job_data(
                    qp_doc=qp_docs[j_idx],
                    regions=job_regions[j_idx],
                    answers=answers,
                    qs=qs,
                    cfg=cfg,
                    exam_key=exam_key,
                    paper_label=paper_lbl,
                    expl_pdf_path=expl_pdf_path,
                )
                if prepared:
                    mcq_prepared[j_idx] = prepared

            ms_info.append(info)

        # ── Phase 2: single batched AI call for all MCQ papers ─────────────────
        mcq_explanations_map: dict[int, dict] = {}
        if mcq_prepared:
            sorted_indices = sorted(mcq_prepared.keys())
            n_mcq = len(sorted_indices)
            print(f"\nGenerating AI explanations ({n_mcq} MCQ paper(s), 1 API call)…")
            paper_data_list = [mcq_prepared[i] for i in sorted_indices]
            batch_results = batch_generate_mcq_explanations(paper_data_list)
            for idx, expl in zip(sorted_indices, batch_results):
                mcq_explanations_map[idx] = expl

        # ── Phase 3: generate strips for all papers ────────────────────────────
        for j_idx, job in enumerate(jobs):
            info = ms_info[j_idx]
            if info is None:
                continue
            ms_doc = info["doc"]
            ms_type = info["type"]
            qs = info["qs"]
            paper_lbl = info["label"]

            if ms_type == "mcq":
                answers = info["answers"]
                if j_idx in mcq_prepared:
                    expl = mcq_explanations_map.get(j_idx, {})
                    explanation_strips = finalize_mcq_explanation_strips(
                        mcq_prepared[j_idx], expl
                    )
                else:
                    explanation_strips = []

                if explanation_strips:
                    mstrips: list[Strip] = explanation_strips
                    for s in explanation_strips:
                        if isinstance(s, VectorStrip) and s.src_doc not in ms_docs:
                            ms_docs.append(s.src_doc)
                            break
                else:
                    mstrips = create_mcq_answer_strips(answers, qs)
            else:
                ms_regions = find_ms_answer_regions(ms_doc, qs, cfg)
                if not ms_regions:
                    print("  No mark scheme regions found.")
                    continue
                print(
                    f"  Extracting mark scheme for questions {sorted(set(r[0] for r in ms_regions))} "
                    f"({len(ms_regions)} region(s))"
                )
                mstrips = collect_vector_strips(ms_doc, ms_regions, is_ms=True, cfg=cfg)

            if not mstrips:
                continue

            if use_paper_sublabels:
                if all_ms_strips:
                    all_ms_strips.append(GapStrip(height_pt=8.0))
                all_ms_strips.append(paper_lbl)
            elif len(jobs) > 1 and all_ms_strips:
                all_ms_strips.append(GapStrip(height_pt=8.0))

            all_ms_strips.extend(mstrips)

        if all_ms_strips:
            answer_anchors = layout_vector_strips_to_pdf(
                all_ms_strips, str(answers_path), page_header,
            )
            print(f"\n  Saved: {answers_path}")

        print("\nExercise sheet n-up variants (pdfjam)…")
        run_exercise_sheet_pdfjam_variants(out_path)
        if all_ms_strips:
            run_exercise_sheet_pdfjam_variants(answers_path)

        print("\nGenerating difficulty ranking…")
        generate_difficulty_ranking(
            exercise_pdf=out_path,
            answer_pdf=answers_path if (all_ms_strips and answers_path.exists()) else None,
            out_path=out_path.parent,
            name=out_path.stem,
        )

    finally:
        for d in qp_docs + ms_docs:
            try:
                d.close()
            except Exception:
                pass

    overview = build_exercise_overview(exercise_anchors)
    if answer_anchors:
        merge_answer_anchors_into_overview(overview, answer_anchors)
    merge_mcq_flags_into_overview(
        overview, jobs, job_mcq_ms, use_paper_sublabels=use_paper_sublabels
    )
    print("\nDone!")
    return overview


def run_extraction(input_pdf: str, output_pdf: str, requested: list, ms_pdf: str | None):
    run_extraction_jobs(
        [{"input_pdf": input_pdf, "questions": requested, "mark_scheme_pdf": ms_pdf}],
        output_pdf,
        exam_key=None,
    )
