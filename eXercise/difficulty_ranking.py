# -*- coding: utf-8 -*-
"""Generate a difficulty-ranking PDF for an exercise sheet.

Call :func:`generate_difficulty_ranking` at the end of a pipeline run.

The function:
1. Sends both the exercise PDF and (optionally) the answer PDF as images
   to an LLM (default: RANKING_MODEL env var, falls back to AI_DEFAULT_MODEL).
2. Parses the returned numbered list of question identifiers.
3. Typesets the ranked list with pdflatex.
4. Saves ``{name}_ranking.pdf`` next to the exercise sheet.

Set ``RANKING_SKIP=true`` in ``.env`` / environment to disable silently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import traceback as _traceback
from pathlib import Path

from .latex_utils import latex_escape as _latex_escape
from .ranking_ai import _eprint, _rank_exercises_ai


# ---------------------------------------------------------------------------
# LaTeX generation
# ---------------------------------------------------------------------------

def _generate_ranking_latex(ranking: list[str], title: str) -> str:
    items = "\n".join(f"  \\item {_latex_escape(r)}" for r in ranking)
    escaped_title = _latex_escape(title)
    return rf"""\documentclass[12pt]{{article}}
\usepackage[a4paper, top=2.5cm, bottom=2.5cm, left=2cm, right=2cm]{{geometry}}
\usepackage[T1]{{fontenc}}
\usepackage[utf8]{{inputenc}}
\usepackage{{parskip}}
\usepackage{{enumitem}}
\usepackage{{multicol}}
\begin{{document}}
\setlength{{\columnseprule}}{{0.4pt}}
\begin{{center}}
  {{\LARGE\bfseries Difficulty Ranking}}\\[0.5em]
  {{\large {escaped_title}}}\\[0.2em]
  {{\small most difficult $\rightarrow$ easiest}}
\end{{center}}
\vspace{{1.5em}}
\begin{{multicols}}{{2}}
\begin{{enumerate}}[leftmargin=2em]
{items}
\end{{enumerate}}
\end{{multicols}}
\end{{document}}
"""


# ---------------------------------------------------------------------------
# pdflatex helper
# ---------------------------------------------------------------------------

def _find_pdflatex() -> str | None:
    return shutil.which("pdflatex")


def _compile_ranking_latex(
    tex_source: str, out_pdf: Path, save_tex: Path | None = None
) -> bool:
    pdflatex = _find_pdflatex()
    if not pdflatex:
        _eprint("  Skipping ranking PDF: pdflatex not found.")
        return False

    if save_tex is not None:
        save_tex.write_text(tex_source, encoding="utf-8")
        print(f"  Saved TeX: {save_tex}")

    with tempfile.TemporaryDirectory(prefix="ranking_") as tmp:
        tmp_path = Path(tmp)
        tex_file = tmp_path / "ranking.tex"
        tex_file.write_text(tex_source, encoding="utf-8")
        cmd = [
            pdflatex,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-output-directory", str(tmp_path),
            str(tex_file),
        ]
        for run in range(2):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    cwd=str(tmp_path),
                )
                if result.returncode != 0:
                    # With -halt-on-error, run 1 failure means run 2 will fail
                    # the same way; bail immediately and write the full log
                    # next to the (intended) output PDF for debugging.
                    log_path = out_pdf.with_suffix(".log")
                    try:
                        log_path.write_text(result.stdout or "", encoding="utf-8")
                        log_loc = f" (full log: {log_path})"
                    except OSError as log_exc:
                        log_loc = f" (could not write log: {log_exc})"
                    log = (result.stdout or "")[-1500:]
                    _eprint(
                        f"  Ranking: pdflatex failed (run {run + 1}){log_loc}\n"
                        f"  …last 1500 chars of stdout:\n{log}"
                    )
                    return False
            except subprocess.TimeoutExpired:
                _eprint("  Ranking: pdflatex timed out.")
                return False
            except OSError as exc:
                _eprint(f"  Ranking: pdflatex error: {exc}")
                return False

        compiled = tmp_path / "ranking.pdf"
        if not compiled.is_file():
            _eprint("  Ranking: pdflatex ran but produced no PDF.")
            return False

        shutil.copy2(str(compiled), str(out_pdf))
        return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_difficulty_ranking(
    exercise_pdf: Path,
    answer_pdf: Path | None,
    out_path: Path,
    name: str,
    stream_thinking: bool = True,
) -> Path | None:
    """Rank questions in *exercise_pdf* from hardest to easiest and save a PDF.

    Parameters
    ----------
    exercise_pdf:
        Path to the 1-up exercise sheet PDF.
    answer_pdf:
        Path to the 1-up answer sheet PDF, or ``None`` if unavailable.
    out_path:
        Directory where ``{name}_ranking.pdf`` will be written.
    name:
        Stem used as the PDF filename and document title.

    Returns the path to the saved PDF, or ``None`` if skipped/failed.
    """
    if os.environ.get("RANKING_SKIP", "").lower() in ("true", "1", "yes"):
        print("  Ranking skipped (RANKING_SKIP=true).")
        return None

    if not exercise_pdf.exists():
        _eprint(f"  Ranking: exercise PDF not found: {exercise_pdf}")
        return None

    save_debug = os.environ.get("SAVE_TEX", "").lower() in ("true", "1", "yes")
    # Images (rendered PDF pages for the fallback path) are always saved for debugging.
    # SAVE_TEX only controls whether the .tex source file is kept.

    print(f"  Calling AI for difficulty ranking ({name})…")
    try:
        ranking = _rank_exercises_ai(exercise_pdf, answer_pdf, save_dir=out_path, stream_thinking=stream_thinking)
    except Exception as exc:
        _eprint(f"  Ranking: unexpected error during AI call: {exc}")
        _eprint(_traceback.format_exc())
        return None

    if not ranking:
        _eprint("  Ranking: no ranking returned; skipping PDF generation.")
        return None

    tex = _generate_ranking_latex(ranking, name)
    dest = out_path / f"{name}_ranking.pdf"
    save_tex = out_path / f"{name}_ranking.tex" if save_debug else None

    print("  Compiling ranking PDF…", flush=True)
    try:
        ok = _compile_ranking_latex(tex, dest, save_tex=save_tex)
    except Exception as exc:
        _eprint(f"  Ranking: LaTeX compilation error: {exc}")
        return None

    if ok:
        print(f"  Saved: {dest}")
        return dest
    return None
