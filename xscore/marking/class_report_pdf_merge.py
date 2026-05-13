"""PDF assembly for the class report: per-student PDF discovery, TOC
generation, outline + named-destination injection, and the xelatex compile
wrapper.

Concentrates everything between "we have per-student PDFs on disk" and "the
combined class-report PDF exists" — the pikepdf concat, the fixed-point TOC
pagination loop, the outline tree, and the named-destination map for TOC
back-links.
"""

from __future__ import annotations

import math
import os
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from xscore.marking.report_latex import _class_toc_to_tex
from xscore.shared.exam_paths import safe_student_name
from xscore.shared.terminal_ui import warn_line


def _xelatex_timeout() -> int:
    """Read XELATEX_TIMEOUT in seconds (default 60). Used by _compile_tex."""
    raw = os.environ.get("XELATEX_TIMEOUT", "60")
    try:
        return max(1, int(raw))
    except ValueError:
        warn_line(f"Invalid XELATEX_TIMEOUT={raw!r} — using default 60s")
        return 60

def _variant_subfolder_for_suffix(suffix: str) -> str:
    """Map a `_merge_pdfs` suffix to the variant subfolder it lives in."""
    return "portrait_2up" if suffix.startswith("portrait_2up") else suffix

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
    scan_rows: list[dict] | None = None,
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
            _class_toc_to_tex(toc_input, exam_name=exam_name, scan_rows=scan_rows),
            encoding="utf-8",
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
    scan_rows: list[dict] | None = None,
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
                    entries, exam_name, output_pdf, class_overview_pages,
                    scan_rows=scan_rows,
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
