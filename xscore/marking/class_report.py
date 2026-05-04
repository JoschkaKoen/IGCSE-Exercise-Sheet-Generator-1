"""Class-level statistics, grade curving, TeX/PDF compilation, review queue.

The largest of the merge-report internals: aggregates per-student summaries
into class artifacts (XML/MD/TeX/PDF), compiles per-student PDFs in parallel
via xelatex, and emits the review queue side-channel.
"""

from __future__ import annotations

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
    artifact_exam_questions_tex_path,
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

def _grade_curve_target() -> int:
    """Read GRADE_CURVE_TARGET (default 80). Used as the env-var fallback
    when the natural-language prompt doesn't override the target."""
    raw = os.environ.get("GRADE_CURVE_TARGET", "80")
    try:
        return int(raw)
    except ValueError:
        warn_line(f"Invalid GRADE_CURVE_TARGET={raw!r} — using default 80")
        return 80


def _effective_curve_target(ctx: Any) -> int:
    """Resolve the curve target for *ctx*.

    Priority: ``ctx.instruction.curved_grade_override`` (if int) → env var
    ``GRADE_CURVE_TARGET`` (default 80).
    """
    instr = getattr(ctx, "instruction", None)
    if instr is not None:
        override = getattr(instr, "curved_grade_override", None)
        if override is not None:
            return int(override)
    return _grade_curve_target()


_TRUE_STRS  = {"true",  "1", "yes", "on"}
_FALSE_STRS = {"false", "0", "no",  "off"}


def _curved_grade_visible(ctx: Any) -> bool:
    """Resolve whether per-student PDFs include the curved % in their header.

    Priority: ``ctx.instruction.curved_grade_visible`` (if bool) → env var
    ``CURVED_GRADE_VISIBLE`` (default true). Unrecognised env values warn
    and fall back to True.
    """
    instr = getattr(ctx, "instruction", None)
    if instr is not None:
        override = getattr(instr, "curved_grade_visible", None)
        if override is not None:
            return bool(override)
    raw = os.environ.get("CURVED_GRADE_VISIBLE", "true").strip().lower()
    if raw in _TRUE_STRS:
        return True
    if raw in _FALSE_STRS:
        return False
    warn_line(f"Invalid CURVED_GRADE_VISIBLE={raw!r} — using default true")
    return True


def _xelatex_timeout() -> int:
    """Read XELATEX_TIMEOUT in seconds (default 60). Used by _compile_tex."""
    raw = os.environ.get("XELATEX_TIMEOUT", "60")
    try:
        return max(1, int(raw))
    except ValueError:
        warn_line(f"Invalid XELATEX_TIMEOUT={raw!r} — using default 60s")
        return 60


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


def _variant_subfolder_for_suffix(suffix: str) -> str:
    """Map a `_merge_pdfs` suffix to the variant subfolder it lives in."""
    return "portrait_2up" if suffix.startswith("portrait_2up") else suffix


# ---------------------------------------------------------------------------
# Combined-PDF assembly with sidebar bookmarks + (optionally) a clickable TOC
# page. The combined PDF is built by `pikepdf` page-concatenation rather than
# a single LaTeX compile, so navigation is added at the PDF level: an outline
# tree (always) and named destinations + a hyperref-rendered TOC page (when
# `with_toc=True`). 2up variants pack two students per page so a "first page"
# is approximate — they get bookmarks but no TOC page.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _StudentEntry:
    """One row of the combined PDF: a student's safe-name → file → location."""
    safe_name: str          # destination key (`stu.<safe_name>`); de-duped on collision
    display_name: str       # original name from `student_summaries`, pre-escape
    pdf_path: Path
    pages: int = 0          # filled in by `_read_page_count`
    start_page: int = 0     # 1-indexed, filled in by `_compute_starts`


_TOC_LINES_PER_PAGE = 50  # 11pt entry on A4 with 2cm margins; one initial estimate


def _read_page_count(pdf_path: Path) -> int:
    """Page count of *pdf_path*; returns 0 on error (warn)."""
    try:
        from pikepdf import Pdf

        with Pdf.open(pdf_path) as src:
            return len(src.pages)
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Could not read page count of {pdf_path.name}: {exc}")
        return 0


def _collect_student_pdfs(
    students_dir: Path,
    variant: str,
    suffix: str,
    student_summaries: list[dict],
) -> list[_StudentEntry]:
    """Build the per-student entry list, driven by *student_summaries*.

    Display names come from `s["name"]` (irreversible from filenames since
    `safe_student_name` is `[^\\w] -> _`), so we always drive from
    summaries and forward-resolve the expected per-variant PDF path. Missing
    files are warned and skipped — they must NOT be listed in the TOC or
    bookmarks, otherwise their destinations would land on the wrong page.
    Sorted by safe_name to match the original glob-by-stem ordering.
    """
    entries: list[_StudentEntry] = []
    seen: dict[str, int] = {}
    for s in student_summaries:
        display = str(s.get("name") or "").strip()
        if not display:
            continue
        safe = safe_student_name(display)
        seen[safe] = seen.get(safe, 0) + 1
        if seen[safe] > 1:
            dest_key = f"{safe}_{seen[safe]}"
            warn_line(
                f"Two students collapse to safe_name={safe!r}; using {dest_key!r} "
                f"as the TOC/bookmark destination key for the duplicate"
            )
        else:
            dest_key = safe
        pdf_path = students_dir / safe / variant / f"{safe}_{suffix}.pdf"
        if not pdf_path.exists():
            warn_line(f"PDF missing, skipping from combined report: {pdf_path.name}")
            continue
        entries.append(_StudentEntry(
            safe_name=dest_key, display_name=display, pdf_path=pdf_path
        ))
    entries.sort(key=lambda e: e.safe_name)
    return entries


def _compute_starts(
    entries: list[_StudentEntry], prefix_pages: int
) -> list[_StudentEntry]:
    """Return new entries with `start_page` (1-indexed) filled in."""
    out: list[_StudentEntry] = []
    cursor = prefix_pages + 1
    for e in entries:
        out.append(replace(e, start_page=cursor))
        cursor += max(1, e.pages)  # treat 0-page reads as 1 to avoid collapsing
    return out


def _render_toc_pdf(
    entries: list[_StudentEntry],
    exam_name: str,
    output_pdf: Path,
    class_overview_pages: int,
) -> tuple[Path | None, int, list[_StudentEntry]]:
    """Render the variant-local TOC PDF; return (toc_pdf_path, toc_pages, entries).

    The TOC PDF count depends on the number of entries, which is fixed
    before compile, so we run a small fixed-point loop: render with a
    page-count estimate, measure, retry. Caps at 3 attempts. The
    visible page numbers shown on the TOC are the merged document's
    physical numbers (which match every PDF viewer's toolbar) — the
    per-student PDF's own internal footer numbering is independent.
    """
    n = len(entries)
    if n == 0:
        return None, 0, _compute_starts(entries, class_overview_pages)

    out_dir = output_pdf.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{output_pdf.stem}_toc.tex"
    pdf_path = out_dir / f"{output_pdf.stem}_toc.pdf"

    est = max(1, math.ceil(n / _TOC_LINES_PER_PAGE))
    starts: list[_StudentEntry] = []
    actual = 0
    for _ in range(3):
        starts = _compute_starts(entries, class_overview_pages + est)
        toc_input = [
            {"safe_name": e.safe_name, "display_name": e.display_name, "page": e.start_page}
            for e in starts
        ]
        tex_path.write_text(
            _class_toc_to_tex(toc_input, exam_name=exam_name), encoding="utf-8"
        )
        _compile_tex(tex_path, out_dir)
        if not pdf_path.exists():
            raise RuntimeError(f"TOC compile produced no PDF at {pdf_path}")
        actual = _read_page_count(pdf_path)
        if actual <= 0 or actual == est:
            break
        est = actual
    else:
        warn_line(
            f"TOC pagination did not converge after 3 attempts "
            f"(est={est}, actual={actual}); page numbers may be off by one"
        )
    return pdf_path, actual or est, starts


def _flatten_to_names_array(items: list[tuple[str, Any]], pdf):
    """Sorted PDF /Names array `[key, value, key, value, …]` for /Names trees."""
    from pikepdf import Array, String

    flat: list[Any] = []
    for key, value in sorted(items, key=lambda kv: kv[0]):
        flat.append(String(key))
        flat.append(value)
    return pdf.make_indirect(Array(flat))


def _inject_outlines_and_dests(
    combined,
    entries: list[_StudentEntry],
    *,
    with_named_dests: bool,
) -> None:
    """Add the bookmark tree and (optionally) `/Names/Dests` for TOC links.

    Two independent try/excepts so a failure in one degrades gracefully:
    bookmarks may succeed even if dest injection fails, and vice versa.
    """
    from pikepdf import Array, Dictionary, Name, OutlineItem

    try:
        with combined.open_outline() as outline:
            for e in entries:
                outline.root.append(
                    OutlineItem(e.display_name, "Fit", e.start_page - 1)
                )
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Could not add bookmarks to combined report: {exc}")

    if not with_named_dests:
        return
    try:
        items: list[tuple[str, Any]] = []
        for e in entries:
            page_obj = combined.pages[e.start_page - 1].obj  # raw Object, not Page helper
            items.append((f"stu.{e.safe_name}", Array([page_obj, Name("/Fit")])))
        names_array = _flatten_to_names_array(items, combined)
        # Dictionary keys are plain strings starting with "/", not Name objects.
        names = combined.Root.get("/Names") or Dictionary()
        names["/Dests"] = combined.make_indirect(Dictionary({"/Names": names_array}))
        combined.Root["/Names"] = combined.make_indirect(names)
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Could not add TOC named destinations: {exc}")


def _merge_pdfs(
    class_pdf: Path,
    students_dir: Path,
    output_pdf: Path,
    suffix: str,
    student_summaries: list[dict],
    exam_name: str = "",
    *,
    with_toc: bool = False,
) -> None:
    """Concatenate the class overview PDF with per-student PDFs + add navigation.

    Always adds an outline (sidebar bookmarks). When *with_toc=True*, also
    inserts a clickable TOC page between the class overview and the first
    student, with named destinations resolving from the TOC links into the
    per-student first pages.

    Failure modes are local: if the TOC compile fails we skip that page and
    still produce bookmarks; if outline injection fails we still save the
    plain merged PDF; if the outer concat fails we warn and produce nothing
    (today's behavior, unchanged).
    """
    variant = _variant_subfolder_for_suffix(suffix)
    entries = _collect_student_pdfs(students_dir, variant, suffix, student_summaries)
    if not entries:
        warn_line(
            f"No student PDFs matched */{variant}/*_{suffix}.pdf — combined "
            f"report {output_pdf.name} will contain only the class overview"
        )

    try:
        from pikepdf import Pdf

        # Read all page counts up front so we can compute start pages.
        class_overview_pages = _read_page_count(class_pdf) if class_pdf.exists() else 0
        if not class_pdf.exists():
            warn_line(f"PDF missing, skipping from combined report: {class_pdf.name}")
        entries = [replace(e, pages=_read_page_count(e.pdf_path)) for e in entries]

        toc_pdf: Path | None = None
        if with_toc and entries:
            try:
                toc_pdf, _toc_pages, entries = _render_toc_pdf(
                    entries, exam_name, output_pdf, class_overview_pages
                )
            except Exception as exc:  # noqa: BLE001
                warn_line(f"Could not render TOC for {output_pdf.name}: {exc}")
                toc_pdf = None
                entries = _compute_starts(entries, class_overview_pages)
        else:
            entries = _compute_starts(entries, class_overview_pages)

        sources: list[Path] = []
        if class_pdf.exists():
            sources.append(class_pdf)
        if toc_pdf is not None and toc_pdf.exists():
            sources.append(toc_pdf)
        sources.extend(e.pdf_path for e in entries)

        combined = Pdf.new()
        for pdf_path in sources:
            with Pdf.open(pdf_path) as src:
                combined.pages.extend(src.pages)

        if entries:
            _inject_outlines_and_dests(
                combined, entries, with_named_dests=(toc_pdf is not None)
            )
        combined.save(output_pdf)
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Could not create combined class report: {exc}")


def _compile_tex(tex_path: Path, output_dir: Path) -> None:
    """Compile .tex with xelatex. Warns on failure but does not raise."""
    try:
        result = subprocess.run(
            [
                "xelatex",
                "-interaction=nonstopmode",
                f"-output-directory={output_dir}",
                str(tex_path),
            ],
            capture_output=True,
            timeout=_xelatex_timeout(),
        )
        if result.returncode != 0:
            warn_line(
                f"xelatex returned {result.returncode} for {tex_path.name} "
                f"— PDF may have errors (see {tex_path.with_suffix('.log').name})"
            )
    except FileNotFoundError:
        warn_line("xelatex not found — PDF reports skipped (install TeX Live or MacTeX)")
    except subprocess.TimeoutExpired:
        warn_line(f"xelatex timed out for {tex_path.name}")
    except Exception as exc:  # noqa: BLE001
        warn_line(f"xelatex error for {tex_path.name}: {exc}")


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

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


def _apply_grade_curve(student_summaries: list[dict], target: int) -> int:
    """Mutate summaries in place; return the offset actually applied (post-clip).

    Solves for the offset *x* such that ``mean(min(100, raw + x)) == target``
    so the curved class mean lands on the target even when top students would
    overflow the 100% cap. The naive ``target − raw_mean`` is computed pre-clip;
    when any student's curved score would exceed 100, the per-student
    ``min(100, …)`` truncates the excess and the actual class mean falls below
    target by the lost amount divided by *n*.

    The function ``mean(min(100, raw + x))`` is monotone non-decreasing in *x*,
    so a closed-form iteration over candidate cap-counts ``k ∈ {0..n}`` gives
    an exact answer in at most *n* iterations. For each *k* (top-k students
    capped), with *S* = sum of the *n−k* lowest raws, solve
    ``x = (n·target − 100k − S) / (n−k)``. Accept the *k* where the boundary
    raws are consistent: the (k+1)-th-highest does not exceed ``100−x``, the
    k-th-highest does (when both exist).

    The returned offset is rounded to int for display in ``class_stats.json``;
    the per-student ``curved_pct`` uses the unrounded value for accuracy.
    """
    raws = [s["percentage"] for s in student_summaries if s["percentage"] is not None]
    if not raws:
        for s in student_summaries:
            s["curved_pct"] = None
        return 0
    n = len(raws)
    sorted_desc = sorted(raws, reverse=True)
    offset = target - sum(raws) / n  # k=0 baseline (matches pre-fix behaviour)
    for k in range(n + 1):
        n_unc = n - k
        if n_unc == 0:
            continue
        sum_unc = sum(sorted_desc[k:])
        x = (n * target - 100 * k - sum_unc) / n_unc
        top_unc = sorted_desc[k] if k < n else None
        bot_cap = sorted_desc[k - 1] if k > 0 else None
        if (top_unc is None or top_unc + x <= 100 + 1e-9) and \
           (bot_cap is None or bot_cap + x >= 100 - 1e-9):
            offset = x
            break
    offset = max(0.0, offset)  # never bump anyone *down* — curve is one-way
    for s in student_summaries:
        if s["percentage"] is None:
            s["curved_pct"] = None
        else:
            s["curved_pct"] = min(100, max(0, s["percentage"] + offset))
    return int(round(offset))


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

def _build_class_report(
    ctx: Any,
    student_summaries: list[dict],
    q_totals: dict[str, list[float]],
    exam_name: str,
) -> None:
    """Build and write class XML/MD/TeX/PDF. Runs after both passes."""
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
    _write_class_marks_xlsx(
        class_report=class_report,
        full_reports=getattr(ctx, "full_reports", None) or {},
        scaffold_questions=getattr(ctx.scaffold, "questions", []),
        out_path=artifact_class_marks_xlsx_path(ctx.artifact_dir),
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
    )
    _merge_pdfs(
        tex_path.with_suffix(".pdf"),
        artifact_student_pdfs_dir(ctx.artifact_dir),
        artifact_class_report_combined_portrait_pdf_path(ctx.artifact_dir),
        suffix="portrait",
        student_summaries=student_summaries,
        exam_name=exam_name,
        with_toc=True,
    )

    # With-questions variants — step 29 only emits these when parsed_questions
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


