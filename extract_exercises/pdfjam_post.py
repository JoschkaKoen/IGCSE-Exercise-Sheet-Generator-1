# -*- coding: utf-8 -*-
"""Optional pdfjam n-up variants of the main exercise-sheet PDF."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def run_exercise_sheet_pdfjam_variants(exercise_pdf: Path | str) -> None:
    """Create 4-up (2×2) and 2-up landscape (2×1) siblings next to the exercise PDF.

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
    _run(
        [
            pdfjam,
            "--nup",
            "2x1",
            "--landscape",
            "--paper",
            "a4paper",
            "--frame",
            "true",
            "--scale",
            "1.0",
            "--outfile",
            str(out_2up),
            inp,
        ],
        out_2up,
        "pdfjam 2-up landscape",
    )
