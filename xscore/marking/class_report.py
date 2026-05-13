"""Class-level statistics, grade curving, TeX/PDF compilation, review queue.

The largest of the merge-report internals: aggregates per-student summaries
into class artifacts (XML/MD/TeX/PDF), compiles per-student PDFs in parallel
via xelatex, and emits the review queue side-channel.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from xscore.marking.class_report_export import _write_class_marks_xlsx
from xscore.marking.report_latex import (
    _class_report_to_tex,
    _class_toc_to_tex,
    _exam_questions_to_tex,
    _student_report_list_to_tex,
    _student_report_to_tex,
    _student_report_with_questions_to_tex,
)
from xscore.marking.report_markdown import _class_report_to_md
from xscore.marking.report_xml import class_report_to_xml
from xscore.shared.exam_paths import (
    artifact_class_grade_histogram_curved_path,
    artifact_class_grade_histogram_raw_path,
    artifact_class_marks_xlsx_path,
    artifact_class_question_difficulty_path,
    artifact_class_question_difficulty_top_path,
    artifact_class_report_combined_landscape_pdf_path,
    artifact_class_report_combined_landscape_with_questions_pdf_path,
    artifact_class_report_combined_portrait_2up_pdf_path,
    artifact_class_report_combined_portrait_list_pdf_path,
    artifact_class_report_combined_portrait_pdf_path,
    artifact_class_report_md_path,
    artifact_class_report_pdf_2up_path,
    artifact_class_report_tex_path,
    artifact_class_report_xml_path,
    artifact_class_stats_json_path,
    artifact_exam_questions_tex_path,
    artifact_exam_student_list_json_path,
    artifact_student_pdf_dir,
    artifact_student_pdfs_dir,
    artifact_student_report_pdf_portrait_2up_attempted_path,
    artifact_student_report_pdf_portrait_2up_path,
    artifact_student_report_pdf_portrait_large_attempted_path,
    artifact_student_report_pdf_portrait_large_path,
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
from xscore.marking.student_merge import filter_to_attempted
from eXercise.pdfjam_post import make_2up_landscape_pdf
from xscore.shared.terminal_ui import warn_line


# ---------------------------------------------------------------------------
# Env-var knobs (see default.env "Phase 8 — Reports" section)
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Ranking + PDF/TeX glue
# ---------------------------------------------------------------------------

def _rank_students(students: list[dict]) -> list[dict]:
    """Return students sorted by percentage desc, each dict annotated with 'rank'.

    Ties share the same rank; the next rank skips (1, 2, 2, 4).
    Students with percentage=None sort last and receive rank=None.
    """
    sorted_s = sorted(
        students,
        key=lambda s: s["percentage"] if s["percentage"] is not None else -1,
        reverse=True,
    )
    rank = 1
    for i, s in enumerate(sorted_s):
        if i == 0:
            s["rank"] = rank if s["percentage"] is not None else None
        elif s["percentage"] is None:
            s["rank"] = None
        else:
            if s["percentage"] != sorted_s[i - 1]["percentage"]:
                rank = i + 1
            s["rank"] = rank
    return sorted_s




# ---------------------------------------------------------------------------
# Combined-PDF assembly with sidebar bookmarks + (optionally) a clickable TOC
# page. The combined PDF is built by `pikepdf` page-concatenation rather than
# a single LaTeX compile, so navigation is added at the PDF level: an outline
# tree (always) and named destinations + a hyperref-rendered TOC page (when
# `with_toc=True`). 2up variants pack two students per page so a "first page"
# is approximate — they get bookmarks but no TOC page.
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def _load_scan_page_ranges(artifact_dir: Path) -> list[dict]:
    """Build the second-TOC rows from ``15_student_names/exam_student_list.json``.

    Returns a list of ``{"display_name", "page_range"}`` dicts ordered by the
    student's first scan page — i.e. the same ordering as ai_marking's terminal
    table. Returns ``[]`` if the JSON is missing or unreadable so the second
    TOC silently disappears rather than blocking the class report.
    """
    list_path = artifact_exam_student_list_json_path(artifact_dir)
    if not list_path.is_file():
        return []
    try:
        raw = json.loads(list_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Could not load scan page ranges from {list_path.name}: {exc}")
        return []
    rows: list[dict] = []
    for a in sorted(raw, key=lambda x: (x.get("page_numbers") or [0])[0]):
        pages = a.get("page_numbers") or []
        if not pages:
            continue
        page_range = f"{pages[0]}–{pages[-1]}" if pages[0] != pages[-1] else str(pages[0])
        rows.append({"display_name": a["student_name"], "page_range": page_range})
    return rows


def _build_class_report(
    ctx: Any,
    student_summaries: list[dict],
    q_totals: dict[str, list[float]],
    exam_name: str,
) -> None:
    """Build and write class XML/MD/TeX/PDF. Runs after both passes."""
    scan_rows = _load_scan_page_ranges(ctx.artifact_dir)
    total_max_marks = ctx.scaffold.total_marks
    leaf_avgs = {k: round(sum(v) / len(v), 1) for k, v in q_totals.items()}
    all_avgs, all_max = _build_all_question_tables(
        getattr(ctx.scaffold, "questions", []), leaf_avgs
    )
    known_pcts_sorted = sorted(
        s["percentage"] for s in student_summaries if s["percentage"] is not None
    )
    class_avg = (
        int(round(sum(known_pcts_sorted) / len(known_pcts_sorted)))
        if known_pcts_sorted else None
    )
    n_students = len(student_summaries)
    median_pct = known_pcts_sorted[len(known_pcts_sorted) // 2] if known_pcts_sorted else None
    min_pct = known_pcts_sorted[0] if known_pcts_sorted else None
    max_pct = known_pcts_sorted[-1] if known_pcts_sorted else None
    per_question_pct: dict[str, int] = {
        qnum: int(round(avg / all_max[qnum] * 100))
        for qnum, avg in all_avgs.items()
        if all_max.get(qnum, 0) > 0
    }
    # Leaf-only percentages for the difficulty chart (parents would double-count).
    leaf_pct: dict[str, int] = {
        qnum: int(round(avg / all_max[qnum] * 100))
        for qnum, avg in leaf_avgs.items()
        if all_max.get(qnum, 0) > 0
    }

    # Top-level-only subset for the new ranking. Replicates the `_N`
    # duplicate-suffix convention from `_build_all_question_tables` so the
    # keys line up with `all_avgs` / `all_max` / `per_question_pct`.
    top_keys: list[str] = []
    seen_top: dict[str, int] = {}
    for q in getattr(ctx.scaffold, "questions", []):
        num = str(q.number or "")
        if not num:
            continue
        seen_top[num] = seen_top.get(num, 0) + 1
        top_keys.append(num if seen_top[num] == 1 else f"{num}_{seen_top[num]}")
    top_avgs = {k: all_avgs[k] for k in top_keys if k in all_avgs}
    top_max  = {k: all_max[k]  for k in top_keys if k in all_max}
    top_pct  = {k: per_question_pct[k] for k in top_keys if k in per_question_pct}

    # Charts are best-effort: if matplotlib isn't installed the LaTeX
    # figure block stays empty (template uses ``<% if histogram_*_path %>``).
    histogram_raw_path: str | None = None
    histogram_curved_path: str | None = None
    difficulty_path: str | None = None
    difficulty_top_path: str | None = None
    try:
        from xscore.marking.class_charts import (
            render_grade_histogram, render_question_difficulty,
        )
        h_raw = render_grade_histogram(
            student_summaries,
            artifact_class_grade_histogram_raw_path(ctx.artifact_dir),
            kind="raw",
        )
        if h_raw is not None:
            histogram_raw_path = str(h_raw)
        h_curved = render_grade_histogram(
            student_summaries,
            artifact_class_grade_histogram_curved_path(ctx.artifact_dir),
            kind="curved",
        )
        if h_curved is not None:
            histogram_curved_path = str(h_curved)
        d = render_question_difficulty(
            leaf_pct,
            artifact_class_question_difficulty_path(ctx.artifact_dir),
            kind="leaves",
        )
        if d is not None:
            difficulty_path = str(d)
        d_top = render_question_difficulty(
            top_pct,
            artifact_class_question_difficulty_top_path(ctx.artifact_dir),
            kind="top",
        )
        if d_top is not None:
            difficulty_top_path = str(d_top)
    except ImportError:
        warn_line("matplotlib not installed — class report figures skipped")
    except Exception as exc:  # noqa: BLE001
        warn_line(f"class chart rendering failed: {type(exc).__name__}: {exc}")

    class_report = {
        "students": _rank_students(student_summaries),
        "per_question_averages": all_avgs,
        "per_question_max_marks": all_max,
        "per_question_pct_averages": per_question_pct,
        "per_top_question_averages": top_avgs,
        "per_top_question_max_marks": top_max,
        "per_top_question_pct_averages": top_pct,
        "class_average_pct": class_avg,
        "total_max_marks": total_max_marks,
        "n_students": n_students,
        "median_pct": median_pct,
        "min_pct": min_pct,
        "max_pct": max_pct,
        "histogram_raw_path": histogram_raw_path,
        "histogram_curved_path": histogram_curved_path,
        "difficulty_path": difficulty_path,
        "difficulty_top_path": difficulty_top_path,
    }
    artifact_class_report_xml_path(ctx.artifact_dir).write_text(
        class_report_to_xml(class_report), encoding="utf-8"
    )
    artifact_class_report_md_path(ctx.artifact_dir).write_text(
        _class_report_to_md(class_report), encoding="utf-8"
    )
    # Curve target written to class_stats.json by build_class_stats; pull it
    # out so the workbook's editable curve cell starts from the same value.
    curve_target_pct: int | None = None
    try:
        stats = json.loads(
            artifact_class_stats_json_path(ctx.artifact_dir).read_text(encoding="utf-8")
        )
        ct = stats.get("curve_target")
        if isinstance(ct, int):
            curve_target_pct = ct
    except (OSError, ValueError):
        pass
    _write_class_marks_xlsx(
        class_report=class_report,
        full_reports=getattr(ctx, "full_reports", None) or {},
        scaffold_questions=getattr(ctx.scaffold, "questions", []),
        out_path=artifact_class_marks_xlsx_path(ctx.artifact_dir),
        curve_target_pct=curve_target_pct,
    )
    tex_path = artifact_class_report_tex_path(ctx.artifact_dir)
    tex_path.write_text(_class_report_to_tex(class_report, exam_name=exam_name), encoding="utf-8")
    _compile_tex(tex_path, tex_path.parent)
    _merge_pdfs(
        tex_path.with_suffix(".pdf"),
        artifact_student_pdfs_dir(ctx.artifact_dir),
        artifact_class_report_combined_landscape_pdf_path(ctx.artifact_dir),
        suffix="landscape",
        student_summaries=student_summaries,
        exam_name=exam_name,
        with_toc=True,
        scan_rows=scan_rows,
    )
    _merge_pdfs(
        tex_path.with_suffix(".pdf"),
        artifact_student_pdfs_dir(ctx.artifact_dir),
        artifact_class_report_combined_portrait_pdf_path(ctx.artifact_dir),
        suffix="portrait",
        student_summaries=student_summaries,
        exam_name=exam_name,
        with_toc=True,
        scan_rows=scan_rows,
    )

    # With-questions variants — ai_marking only emits these when parsed_questions
    # is available, so guard each merge by checking that at least one student
    # PDF of that variant exists. Otherwise the merge would produce a single-
    # page combined PDF with just the class overview, which is misleading.
    students_dir = artifact_student_pdfs_dir(ctx.artifact_dir)
    if any(students_dir.glob("*/landscape_with_questions/*_landscape_with_questions.pdf")):
        _merge_pdfs(
            tex_path.with_suffix(".pdf"),
            students_dir,
            artifact_class_report_combined_landscape_with_questions_pdf_path(ctx.artifact_dir),
            suffix="landscape_with_questions",
            student_summaries=student_summaries,
            exam_name=exam_name,
            with_toc=True,
            scan_rows=scan_rows,
        )
    if any(students_dir.glob("*/portrait_list/*_portrait_list.pdf")):
        _merge_pdfs(
            tex_path.with_suffix(".pdf"),
            students_dir,
            artifact_class_report_combined_portrait_list_pdf_path(ctx.artifact_dir),
            suffix="portrait_list",
            student_summaries=student_summaries,
            exam_name=exam_name,
            with_toc=True,
            scan_rows=scan_rows,
        )

    class_pdf_path = tex_path.with_suffix(".pdf")
    class_2up_path = artifact_class_report_pdf_2up_path(ctx.artifact_dir)
    if class_pdf_path.is_file():
        make_2up_landscape_pdf(class_pdf_path, class_2up_path)
    if class_2up_path.is_file():
        # 2up variants pack two students per page so a "first page" is
        # approximate (off by one when a student's portrait_large is an
        # odd page count) — bookmarks only, no clickable TOC page.
        _merge_pdfs(
            class_2up_path,
            artifact_student_pdfs_dir(ctx.artifact_dir),
            artifact_class_report_combined_portrait_2up_pdf_path(ctx.artifact_dir),
            suffix="portrait_2up",
            student_summaries=student_summaries,
            exam_name=exam_name,
        )
        # Smaller-font combined variants — same class summary prefix,
        # per-student halves come from the *_portrait_2up_<N>pt.pdf files
        # produced by _pass2_write_tex.
        for fs in _EXTRA_2UP_FONT_SIZES:
            _merge_pdfs(
                class_2up_path,
                artifact_student_pdfs_dir(ctx.artifact_dir),
                _suffixed(
                    artifact_class_report_combined_portrait_2up_pdf_path(ctx.artifact_dir),
                    f"_{fs}pt",
                ),
                suffix=f"portrait_2up_{fs}pt",
                student_summaries=student_summaries,
                exam_name=exam_name,
            )



# ---------------------------------------------------------------------------
# Backwards-compat re-exports — curve math and PDF assembly now live in
# sibling modules. External importers (merge_reports, scheme_graphics_check)
# kept working without changing their import paths.
# ---------------------------------------------------------------------------

from xscore.marking.class_report_curve import (  # noqa: E402, F401
    _apply_grade_curve,
    _curved_grade_visible,
    _effective_curve_target,
    _grade_curve_target,
)
from xscore.marking.class_report_tex import (  # noqa: E402, F401
    _build_all_question_tables,
    _ensure_student_pdf_subdirs,
    _pass2_write_tex,
    _suffixed,
)
from xscore.marking.class_report_pdf_merge import (  # noqa: E402, F401
    _collect_student_pdfs,
    _compile_tex,
    _compute_starts,
    _flatten_to_names_array,
    _inject_outlines_and_dests,
    _merge_pdfs,
    _read_page_count,
    _render_toc_pdf,
    _StudentEntry,
    _variant_subfolder_for_suffix,
    _xelatex_timeout,
)
