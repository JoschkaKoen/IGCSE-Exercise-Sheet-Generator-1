# -*- coding: utf-8 -*-
"""Optional pdfjam n-up variants of the main exercise-sheet PDF."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import PDFJAM_NUP_SCALE

# Source page dimensions (A4 portrait, 595 × 842 pt).
_SRC_W, _SRC_H = 595.0, 842.0

# Name-field area in 1-up source coordinates (generous — covers eraser + box + label).
_NF_X0, _NF_Y0 = 0.0,   0.0
_NF_X1, _NF_Y1 = 170.0, 38.0

# IGCSE decorative line in 1-up source coordinates.
_NF_LINE_Y  = 24.85   # y of the horizontal decoration line
_NF_LINE_X0 = 18.0    # left start (= OUTPUT_MARGIN_PT + line pad = 10 + 8)
_NF_LINE_W  = 0.5     # stroke width in 1-up (scaled with the sub-page below)

# Width (pt) of the separator lines drawn between sub-pages.
_SEP_LINE_W = 1.0


def _draw_nup_separators(pdf_path: Path, cols: int, rows: int) -> None:
    """Draw thin black separator lines between sub-pages on every page.

    Generic — no IGCSE-specific touch-ups. Used by ``make_2up_landscape_pdf``
    to give the imposed PDF a clean visual divider down the middle of each
    landscape sheet.
    """
    try:
        import fitz  # PyMuPDF — available in the project venv
    except ImportError:
        return

    doc = fitz.open(str(pdf_path))
    for pg in doc:
        pw = pg.rect.width
        ph = pg.rect.height
        slot_w = pw / cols
        slot_h = ph / rows
        half = _SEP_LINE_W / 2
        for c in range(1, cols):
            x = c * slot_w
            pg.draw_rect(fitz.Rect(x - half, 0, x + half, ph),
                         fill=(0, 0, 0), color=None, width=0)
        for r in range(1, rows):
            y = r * slot_h
            pg.draw_rect(fitz.Rect(0, y - half, pw, y + half),
                         fill=(0, 0, 0), color=None, width=0)
    doc.save(str(pdf_path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    doc.close()


def make_2up_landscape_pdf(
    input_pdf: Path | str,
    output_pdf: Path | str | None = None,
    *,
    draw_separators: bool = True,
) -> Path | None:
    """Produce a 2-up landscape A4 PDF from a portrait A4 input.

    If ``output_pdf`` is None, writes ``<input_stem>_2up.pdf`` next to the
    input. Returns the output path on success, or None if pdfjam is missing
    or fails. Optionally draws thin separator lines between sub-pages.

    Generic helper — no IGCSE-specific post-processing — safe for arbitrary
    portrait PDFs (e.g. xscore student reports).
    """
    inp = Path(input_pdf)
    if not inp.is_file():
        return None

    pdfjam = shutil.which("pdfjam")
    if not pdfjam:
        print("  pdfjam not found on PATH; skipping 2-up landscape variant.")
        return None

    out = (Path(output_pdf) if output_pdf is not None
           else inp.parent / f"{inp.stem}_2up{inp.suffix}")

    try:
        subprocess.run(
            [pdfjam, "--nup", "2x1", "--landscape", "--paper", "a4paper",
             "--frame", "false", "--scale", str(PDFJAM_NUP_SCALE),
             "--outfile", str(out), str(inp)],
            check=True, capture_output=True, text=True,
        )
    except FileNotFoundError:
        print("  Warning: pdfjam 2-up landscape: pdfjam executable disappeared.")
        return None
    except subprocess.CalledProcessError as e:
        err = (e.stderr or e.stdout or str(e))[:500]
        print(f"  Warning: pdfjam 2-up landscape failed ({e.returncode}): {err.strip()}")
        return None

    if not out.is_file():
        return None

    if draw_separators:
        _draw_nup_separators(out, cols=2, rows=1)
    return out


def _fix_nup_name_fields(pdf_path: Path, cols: int, rows: int,
                         *, sub_scale: float = 1.0) -> None:
    """Remove duplicate name fields, restore the IGCSE line, and draw sub-page separators.

    In the 1-up source every page carries the name field.  After pdfjam tiles
    those pages the name field appears in every sub-page.  This function:
      • covers the name-field area with white in sub-pages 2+ (erases name box)
      • redraws the IGCSE decorative line through that same band so only the
        name box disappears — the line itself is seamlessly restored.
      • draws separator lines between sub-pages (vertical column boundaries and
        horizontal row boundaries) so no outer border is needed from pdfjam.
    Sub-page 1 (top-left, col=0 row=0) is left untouched.

    ``sub_scale`` mirrors the ``--scale`` passed to pdfjam: when < 1.0, each
    sub-page is shrunk and the whole cols×rows grid is centred on the page,
    so the eraser/IGCSE-line geometry has to track that inset.  Default 1.0
    reproduces the original edge-to-edge behaviour byte-for-byte.
    """
    try:
        import fitz  # PyMuPDF — available in the project venv
    except ImportError:
        return

    doc = fitz.open(str(pdf_path))
    for pg in doc:
        pw = pg.rect.width
        ph = pg.rect.height
        slot_w = pw / cols
        slot_h = ph / rows
        # pdfjam fits each subpage uniformly to its slot (= page / nup), applies
        # the user --scale, then packs the cols×rows grid centred on the page
        # with no internal gaps.  Mirror that geometry here.
        fit = min(slot_w / _SRC_W, slot_h / _SRC_H)
        s = fit * sub_scale                    # source-pt → page-pt
        sub_w = _SRC_W * s
        sub_h = _SRC_H * s
        grid_dx = (pw - cols * sub_w) / 2
        grid_dy = (ph - rows * sub_h) / 2
        lw = _NF_LINE_W * s                    # scale line width with the sub-page

        # Erase duplicate name fields and restore IGCSE lines first, then draw
        # separators last so they are always painted on top.
        for row in range(rows):
            for col in range(cols):
                if row == 0 and col == 0:
                    continue  # keep sub-page 1 intact
                ox = grid_dx + col * sub_w
                oy = grid_dy + row * sub_h
                # 1. Erase the name-field area with white (no stroke so the border
                #    doesn't bleed onto the separator line)
                pg.draw_rect(
                    fitz.Rect(
                        ox + _NF_X0 * s, oy + _NF_Y0 * s,
                        ox + _NF_X1 * s, oy + _NF_Y1 * s,
                    ),
                    fill=(1, 1, 1), color=None, width=0,
                )
                # 2. Restore the IGCSE decorative line across the erased band
                ly  = oy + _NF_LINE_Y  * s
                lx0 = ox + _NF_LINE_X0 * s
                lx1 = ox + _NF_X1      * s
                pg.draw_line(
                    fitz.Point(lx0, ly), fitz.Point(lx1, ly),
                    color=(0, 0, 0), width=lw,
                )

        # Draw separator lines between sub-pages as filled rectangles (crisp,
        # no anti-aliasing artefacts from stroked paths) painted last so they
        # are never partially covered by page content.
        half = _SEP_LINE_W / 2
        for c in range(1, cols):
            x = c * slot_w
            pg.draw_rect(fitz.Rect(x - half, 0, x + half, ph),
                         fill=(0, 0, 0), color=None, width=0)
        for r in range(1, rows):
            y = r * slot_h
            pg.draw_rect(fitz.Rect(0, y - half, pw, y + half),
                         fill=(0, 0, 0), color=None, width=0)

    doc.save(str(pdf_path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    doc.close()


def run_exercise_sheet_pdfjam_variants(
    exercise_pdf: Path | str,
) -> None:
    """Create 4-up (2×2) and 2-up landscape (2×1) siblings next to the exercise PDF.

    Both variants are built with ``--frame false``; separator lines between
    sub-pages are drawn in post-processing by ``_fix_nup_name_fields``.

    Requires ``pdfjam`` on ``PATH`` (TeX Live / MacTeX).  Failures are logged; extraction
    still succeeds without these files.
    """
    import concurrent.futures

    path = Path(exercise_pdf).resolve()
    if not path.is_file():
        return

    pdfjam = shutil.which("pdfjam")
    if not pdfjam:
        print("  pdfjam not found on PATH; skipping 4-up / 2-up exercise-sheet variants.")
        return

    inp = str(path)
    stem = path.stem
    suf = path.suffix
    parent = path.parent
    out_4up = parent / f"{stem}_4up{suf}"
    out_2up = parent / f"{stem}_2up{suf}"

    def _run(args: list[str], out: Path, label: str) -> None:
        try:
            subprocess.run(
                args,
                check=True,
                capture_output=True,
                text=True,
            )
            print(f"  Saved: {out}")
        except FileNotFoundError:
            print(f"  Warning: {label}: pdfjam executable disappeared.")
        except subprocess.CalledProcessError as e:
            err = (e.stderr or e.stdout or str(e))[:500]
            print(f"  Warning: {label} failed ({e.returncode}): {err.strip()}")

    def _run_4up() -> None:
        _run(
            [pdfjam, "--nup", "2x2", "--frame", "false", "--scale", "1.0",
             "--outfile", str(out_4up), inp],
            out_4up, "pdfjam 4-up",
        )
        if out_4up.is_file():
            _fix_nup_name_fields(out_4up, cols=2, rows=2)

    def _run_2up() -> None:
        _run(
            [pdfjam, "--nup", "2x1", "--landscape", "--paper", "a4paper",
             "--frame", "false", "--scale", str(PDFJAM_NUP_SCALE),
             "--outfile", str(out_2up), inp],
            out_2up, "pdfjam 2-up landscape",
        )
        if out_2up.is_file():
            _fix_nup_name_fields(out_2up, cols=2, rows=1,
                                 sub_scale=PDFJAM_NUP_SCALE)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_4up = ex.submit(_run_4up)
        fut_2up = ex.submit(_run_2up)
        # Re-raise any exceptions from workers
        fut_4up.result()
        fut_2up.result()
