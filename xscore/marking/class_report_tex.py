"""TeX generation for the class report: per-question stats tables, the
pass-2 TeX writer that lays out every per-student layout variant, and the
small filesystem helpers (subfolder creation + filename suffixing) it uses.

Extracted from ``class_report`` so the orchestrator stays focused on
sequencing rather than on the per-variant TeX rendering details.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from xscore.marking.report_latex import (
    _exam_questions_to_tex,
    _student_report_list_to_tex,
    _student_report_to_tex,
    _student_report_with_questions_to_tex,
)
from xscore.marking.student_merge import filter_to_attempted
from xscore.shared.exam_paths import (
    artifact_exam_questions_tex_path,
    artifact_student_pdfs_dir,
    artifact_student_report_tex_landscape_attempted_path,
    artifact_student_report_tex_landscape_path,
    artifact_student_report_tex_landscape_with_questions_attempted_path,
    artifact_student_report_tex_landscape_with_questions_path,
    artifact_student_report_tex_portrait_attempted_path,
    artifact_student_report_tex_portrait_large_attempted_path,
    artifact_student_report_tex_portrait_large_path,
    artifact_student_report_tex_portrait_list_attempted_path,
    artifact_student_report_tex_portrait_list_path,
    artifact_student_report_tex_portrait_path,
    safe_student_name,
)
from xscore.shared.terminal_ui import warn_line


def _build_all_question_tables(
    questions: list,
    leaf_avgs: dict[str, float],
) -> tuple[dict[str, float], dict[str, int]]:
    """Return (all_avgs, all_max) for every question node including parents.

    Leaf averages come directly from leaf_avgs (keyed with _N suffixes for duplicates).
    Parent averages are the rounded sum of their direct children's averages (recursive).
    all_max is keyed with the same _N suffix convention using a seen counter.
    """
    from xscore.shared.models import flatten_questions

    def _subtree_avg(q) -> float | None:
        if not q.subquestions:
            return leaf_avgs.get(str(q.number or ""))
        parts = [_subtree_avg(c) for c in q.subquestions]
        valid = [p for p in parts if p is not None]
        return round(sum(valid), 1) if valid else None

    all_avgs: dict[str, float] = dict(leaf_avgs)
    all_max: dict[str, int] = {}
    seen: dict[str, int] = {}
    for q in flatten_questions(questions):
        num = str(q.number or "")
        if not num:
            continue
        seen[num] = seen.get(num, 0) + 1
        key = num if seen[num] == 1 else f"{num}_{seen[num]}"
        all_max[key] = int(q.marks or 0)
        if q.subquestions:
            avg = _subtree_avg(q)
            if avg is not None:
                all_avgs[key] = avg
    return all_avgs, all_max


# ---------------------------------------------------------------------------
# Pass 2 — per-student .tex files + parallel xelatex compile
# ---------------------------------------------------------------------------

# Extra portrait_large font sizes to render alongside the canonical 12pt
# variant. Each size produces an additional `<name>_portrait_large_<N>pt.tex`
# (compiled in the same parallel xelatex batch), `<name>_portrait_2up_<N>pt.pdf`
# (made by the same parallel pdfjam batch), and a class-level
# `class_report_combined_portrait_2up_<N>pt.pdf` (built by `_build_class_report`).
# The canonical 12pt artifacts keep their bare names — only the extras carry
# a size suffix. Adding a 9pt variant later is a one-line edit.
_EXTRA_2UP_FONT_SIZES: tuple[int, ...] = (10, 11)


# Header subtitle injected into every per-student `_attempted` variant so a
# teacher reading the PDF/MD knows which version they're looking at.
_ATTEMPTED_SUBTITLE = "showing attempted questions only"


def _suffixed(p: Path, suffix: str) -> Path:
    """Return *p* with *suffix* inserted before its extension. ``suffix=""`` is a no-op."""
    return p if not suffix else p.with_name(p.stem + suffix + p.suffix)


def _ensure_student_pdf_subdirs(
    artifact_dir: Path, student: str, *, with_questions: bool
) -> None:
    """Create the per-student variant subfolders that ``_pass2_write_tex``
    will write into. Idempotent.

    Each base variant gets a sibling ``<variant>_attempted`` subfolder for
    the questions-the-student-answered renderings produced alongside the
    canonical ones."""
    base_variants = ["landscape", "portrait", "portrait_large", "portrait_2up"]
    if with_questions:
        base_variants += ["landscape_with_questions", "portrait_list"]
    variants = base_variants + [f"{v}_attempted" for v in base_variants]
    student_dir = artifact_student_pdf_dir(artifact_dir, student)
    for v in variants:
        (student_dir / v).mkdir(parents=True, exist_ok=True)


def _pass2_write_tex(
    student_summaries: list[dict],
    full_reports: dict[str, dict],
    artifact_dir: Path,
    exam_name: str,
    workers: int,
    show_curved_grade: bool = True,
    parsed_questions: list[dict] | None = None,
    qmap_by_num: dict[str, dict] | None = None,
    name_suffix: str = "",
    class_avg: int | None = None,
    q_to_graphics: dict[str, list[str]] | None = None,
    scheme_graphics_dir: str = "",
    is_all_mcq: bool = False,
) -> None:
    """Write per-student .tex files (landscape + portrait + portrait-large), then compile all in parallel.

    When ``parsed_questions`` is non-None, additionally writes each student's
    ``_landscape_with_questions.tex`` and ``_portrait_list.tex`` plus the
    run-level ``exam_questions.tex`` — all compiled in the same parallel pass.

    *name_suffix* is appended to every per-student output filename (before
    the extension). Default ``""`` is the only value used by current callers;
    the parameter is kept for the inner ``_{fs}pt`` font-size loop and for
    callers that want to render an alternate batch into the same folder.
    """
    qmap_by_num = qmap_by_num or {}
    q_to_graphics = q_to_graphics or {}
    tex_paths: list[Path] = []
    for s in student_summaries:
        report = full_reports[s["name"]]
        report["curved_pct"] = s["curved_pct"]
        # Pre-filter once for all the `_attempted` variants below.
        attempted_report = filter_to_attempted(report)
        _ensure_student_pdf_subdirs(
            artifact_dir, s["name"], with_questions=parsed_questions is not None
        )
        for orientation, path_fn, att_path_fn, font_size in (
            ("landscape", artifact_student_report_tex_landscape_path,
             artifact_student_report_tex_landscape_attempted_path, 10),
            ("portrait",  artifact_student_report_tex_portrait_path,
             artifact_student_report_tex_portrait_attempted_path, 10),
            ("portrait",  artifact_student_report_tex_portrait_large_path,
             artifact_student_report_tex_portrait_large_attempted_path, 12),
        ):
            tex_path = _suffixed(path_fn(artifact_dir, s["name"]), name_suffix)
            tex_path.write_text(
                _student_report_to_tex(
                    report, exam_name=exam_name, orientation=orientation,
                    font_size=font_size, show_curved_grade=show_curved_grade,
                    class_avg=class_avg,
                    q_to_graphics=q_to_graphics,
                    scheme_graphics_dir=scheme_graphics_dir,
                    is_all_mcq=is_all_mcq,
                ),
                encoding="utf-8",
            )
            tex_paths.append(tex_path)

            att_tex_path = _suffixed(att_path_fn(artifact_dir, s["name"]), name_suffix)
            att_tex_path.write_text(
                _student_report_to_tex(
                    attempted_report, exam_name=exam_name, orientation=orientation,
                    font_size=font_size, show_curved_grade=show_curved_grade,
                    class_avg=class_avg,
                    q_to_graphics=q_to_graphics,
                    scheme_graphics_dir=scheme_graphics_dir,
                    subtitle=_ATTEMPTED_SUBTITLE,
                    is_all_mcq=is_all_mcq,
                ),
                encoding="utf-8",
            )
            tex_paths.append(att_tex_path)

        # Extra portrait_large variants at smaller font sizes for the
        # combined 2up class PDF. Skipped when an outer suffix is present
        # (e.g. an alternate batch) so the portrait_2up_<N>pt glob in
        # _build_class_report doesn't pick up duplicates.
        if not name_suffix:
            for fs in _EXTRA_2UP_FONT_SIZES:
                tex_path = _suffixed(
                    artifact_student_report_tex_portrait_large_path(artifact_dir, s["name"]),
                    f"_{fs}pt",
                )
                tex_path.write_text(
                    _student_report_to_tex(
                        report, exam_name=exam_name, orientation="portrait",
                        font_size=fs, show_curved_grade=show_curved_grade,
                        class_avg=class_avg,
                        q_to_graphics=q_to_graphics,
                        scheme_graphics_dir=scheme_graphics_dir,
                        is_all_mcq=is_all_mcq,
                    ),
                    encoding="utf-8",
                )
                tex_paths.append(tex_path)

        if parsed_questions is not None:
            wq_tex_path = _suffixed(
                artifact_student_report_tex_landscape_with_questions_path(
                    artifact_dir, s["name"]
                ),
                name_suffix,
            )
            wq_tex_path.write_text(
                _student_report_with_questions_to_tex(
                    report, qmap_by_num, exam_name=exam_name,
                    font_size=10, show_curved_grade=show_curved_grade,
                    class_avg=class_avg,
                    q_to_graphics=q_to_graphics,
                    scheme_graphics_dir=scheme_graphics_dir,
                    is_all_mcq=is_all_mcq,
                ),
                encoding="utf-8",
            )
            tex_paths.append(wq_tex_path)

            wq_att_tex_path = _suffixed(
                artifact_student_report_tex_landscape_with_questions_attempted_path(
                    artifact_dir, s["name"]
                ),
                name_suffix,
            )
            wq_att_tex_path.write_text(
                _student_report_with_questions_to_tex(
                    attempted_report, qmap_by_num, exam_name=exam_name,
                    font_size=10, show_curved_grade=show_curved_grade,
                    class_avg=class_avg,
                    q_to_graphics=q_to_graphics,
                    scheme_graphics_dir=scheme_graphics_dir,
                    subtitle=_ATTEMPTED_SUBTITLE,
                    is_all_mcq=is_all_mcq,
                ),
                encoding="utf-8",
            )
            tex_paths.append(wq_att_tex_path)

            list_tex_path = _suffixed(
                artifact_student_report_tex_portrait_list_path(
                    artifact_dir, s["name"]
                ),
                name_suffix,
            )
            list_tex_path.write_text(
                _student_report_list_to_tex(
                    report, qmap_by_num, exam_name=exam_name,
                    show_curved_grade=show_curved_grade,
                    class_avg=class_avg,
                    q_to_graphics=q_to_graphics,
                    scheme_graphics_dir=scheme_graphics_dir,
                ),
                encoding="utf-8",
            )
            tex_paths.append(list_tex_path)

            list_att_tex_path = _suffixed(
                artifact_student_report_tex_portrait_list_attempted_path(
                    artifact_dir, s["name"]
                ),
                name_suffix,
            )
            list_att_tex_path.write_text(
                _student_report_list_to_tex(
                    attempted_report, qmap_by_num, exam_name=exam_name,
                    show_curved_grade=show_curved_grade,
                    class_avg=class_avg,
                    q_to_graphics=q_to_graphics,
                    scheme_graphics_dir=scheme_graphics_dir,
                    subtitle=_ATTEMPTED_SUBTITLE,
                ),
                encoding="utf-8",
            )
            tex_paths.append(list_att_tex_path)

    # The standalone exam-questions PDF is per-run, not per-student. Only
    # emit it when we're rendering the unsuffixed batch.
    if parsed_questions is not None and not name_suffix:
        eq_tex_path = artifact_exam_questions_tex_path(artifact_dir)
        eq_tex_path.parent.mkdir(parents=True, exist_ok=True)
        eq_tex_path.write_text(
            _exam_questions_to_tex(parsed_questions, exam_name=exam_name),
            encoding="utf-8",
        )
        tex_paths.append(eq_tex_path)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda p: _compile_tex(p, p.parent), tex_paths))

    portrait_2up_jobs: list[tuple[Path, Path]] = []
    for s in student_summaries:
        p_in = _suffixed(
            artifact_student_report_pdf_portrait_large_path(artifact_dir, s["name"]),
            name_suffix,
        )
        p_out = _suffixed(
            artifact_student_report_pdf_portrait_2up_path(artifact_dir, s["name"]),
            name_suffix,
        )
        if p_in.is_file():
            portrait_2up_jobs.append((p_in, p_out))

        # Mirror the 2up post-processing for the `_attempted` variant: input
        # `<S>_portrait_large_attempted.pdf` → output `<S>_portrait_2up_attempted.pdf`.
        p_in_att = _suffixed(
            artifact_student_report_pdf_portrait_large_attempted_path(artifact_dir, s["name"]),
            name_suffix,
        )
        p_out_att = _suffixed(
            artifact_student_report_pdf_portrait_2up_attempted_path(artifact_dir, s["name"]),
            name_suffix,
        )
        if p_in_att.is_file():
            portrait_2up_jobs.append((p_in_att, p_out_att))

        # Companion 2up jobs for each extra font size. Skipped when an
        # outer suffix is present. Full-only — these feed the combined
        # class 2up PDF, which intentionally doesn't have an _attempted twin.
        if not name_suffix:
            for fs in _EXTRA_2UP_FONT_SIZES:
                p_in_extra = _suffixed(
                    artifact_student_report_pdf_portrait_large_path(artifact_dir, s["name"]),
                    f"_{fs}pt",
                )
                p_out_extra = _suffixed(
                    artifact_student_report_pdf_portrait_2up_path(artifact_dir, s["name"]),
                    f"_{fs}pt",
                )
                if p_in_extra.is_file():
                    portrait_2up_jobs.append((p_in_extra, p_out_extra))
    if portrait_2up_jobs:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda j: make_2up_landscape_pdf(j[0], j[1]), portrait_2up_jobs))


# ---------------------------------------------------------------------------
# Class report assembly
# ---------------------------------------------------------------------------

