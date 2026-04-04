# -*- coding: utf-8 -*-
"""Optional pdfjam n-up variants of the main exercise-sheet PDF."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# Source page dimensions (A4 portrait, 595 × 842 pt).
_SRC_W, _SRC_H = 595.0, 842.0

# Name-field area in 1-up source coordinates (generous — covers eraser + box + label).
_NF_X0, _NF_Y0 = 0.0,   0.0
_NF_X1, _NF_Y1 = 170.0, 38.0

# IGCSE decorative line in 1-up source coordinates.
# line_y = EXAM_LABEL_TOP_PT + (EXAM_LABEL_FONT_PT + 4) - EXAM_LABEL_FONT_PT * 0.35
#        = 15 + 13 - 3.15 = 24.85
_NF_LINE_Y  = 24.85   # y of the horizontal decoration line
_NF_LINE_X0 = 18.0    # left start (= OUTPUT_MARGIN_PT + line pad = 10 + 8)
_NF_LINE_W  = 0.5     # stroke width in 1-up (scaled with the sub-page below)


def _fix_nup_name_fields(pdf_path: Path, cols: int, rows: int) -> None:
    """Remove the name field from sub-pages 2, 3, 4, … and restore the IGCSE line.

    In the 1-up source every page carries the name field.  After pdfjam tiles
    those pages the name field appears in every sub-page.  This function:
      • covers the name-field area with white in sub-pages 2+ (erases name box)
      • redraws the IGCSE decorative line through that same band so only the
        name box disappears — the line itself is seamlessly restored.
    Sub-page 1 (top-left, col=0 row=0) is left untouched.
    """
    try:
        import fitz  # PyMuPDF — available in the project venv
    except ImportError:
        return

    doc = fitz.open(str(pdf_path))
    for pg in doc:
        slot_w = pg.rect.width  / cols
        slot_h = pg.rect.height / rows
        sx = slot_w / _SRC_W
        sy = slot_h / _SRC_H
        lw = _NF_LINE_W * min(sx, sy)   # scale line width with the sub-page
        for row in range(rows):
            for col in range(cols):
                if row == 0 and col == 0:
                    continue  # keep sub-page 1 intact
                ox = col * slot_w
                oy = row * slot_h
                # 1. Erase the name-field area with white
                pg.draw_rect(
                    fitz.Rect(
                        ox + _NF_X0 * sx, oy + _NF_Y0 * sy,
                        ox + _NF_X1 * sx, oy + _NF_Y1 * sy,
                    ),
                    fill=(1, 1, 1), color=(1, 1, 1), width=0,
                )
                # 2. Restore the IGCSE decorative line across the erased band
                ly  = oy + _NF_LINE_Y  * sy
                lx0 = ox + _NF_LINE_X0 * sx
                lx1 = ox + _NF_X1      * sx
                pg.draw_line(
                    fitz.Point(lx0, ly), fitz.Point(lx1, ly),
                    color=(0, 0, 0), width=lw,
                )
    doc.save(str(pdf_path), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    doc.close()


def run_exercise_sheet_pdfjam_variants(
    exercise_pdf: Path | str,
    *,
    frame_2up: bool = True,
) -> None:
    """Create 4-up (2×2) and 2-up landscape (2×1) siblings next to the exercise PDF.

    *frame_2up* controls whether the 2-up variant is built with ``--frame true``.
    Set to ``False`` to omit the separator line between the two pages.

    Requires ``pdfjam`` on ``PATH`` (TeX Live / MacTeX).  Failures are logged; extraction
    still succeeds without these files.
    """
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

    _run(
        [
            pdfjam,
            "--nup",
            "2x2",
            "--frame",
            "true",
            "--scale",
            "1.0",
            "--outfile",
            str(out_4up),
            inp,
        ],
        out_4up,
        "pdfjam 4-up",
    )
    if out_4up.is_file():
        _fix_nup_name_fields(out_4up, cols=2, rows=2)

    _run(
        [
            pdfjam,
            "--nup",
            "2x1",
            "--landscape",
            "--paper",
            "a4paper",
            "--frame",
            "true" if frame_2up else "false",
            "--scale",
            "1.0",
            "--outfile",
            str(out_2up),
            inp,
        ],
        out_2up,
        "pdfjam 2-up landscape",
    )
    if out_2up.is_file():
        _fix_nup_name_fields(out_2up, cols=2, rows=1)
