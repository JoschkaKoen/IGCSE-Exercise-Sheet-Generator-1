"""Verify two xelatex regressions in run 2026-05-10_21-57-27.

File: xscore/marking/report_latex_text.py

Bug A (Andy_2 reports): "There's no line here to end" at every alltt block
  followed by a paragraph break. _protect_alltt's post-restore strip at
  line 443 only consumed ONE \\newline per match because the regex lacked a
  `+` quantifier — Andy_2's Q5a has \\n\\n between three "Error N / Correction
  \\begin{alltt}…\\end{alltt}" groups, which became `\\newline \\newline` after
  \\n→\\newline conversion, and only one was stripped.

Bug B (Lucas reports): "Missing $ inserted" inside an alltt block that
  contains `$\\neq$`. Inside alltt, `$` is catcode 12 (literal char, not
  math-shift) per `\\dospecials` in alltt.sty, so `$\\neq$` does NOT enter
  math mode and `\\neq` (a math-only command) errors. `_ALLTT_MATH_SUB` was
  missing `neq`.

Calls _ai_cell on the exact failing inputs, wraps the output in a minimal
preamble matching the student-PDF preamble, and runs xelatex. Asserts
exit 0 and absence of `! ` error lines in the log.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "/Users/joschka/Desktop/Programming/eXercise")

from xscore.marking.report_latex_cells import _ai_cell


# Bug A: Andy_2 Q5a student answer (verbatim from
# output/xscore/w23_23_Unit_Test/2026-05-10_21-57-27/30_student_report_preparation/Andy_2/Andy_2.yaml)
ANDY_Q5A = (
    "Error 1 9\n"
    "Correction \\begin{alltt}ELSE $\\rightarrow$ NEXT\\end{alltt}\n"
    "\n"
    "Error 2 6\n"
    "Correction \\begin{alltt}Password $\\leftarrow$ New Password\\end{alltt}\n"
    "\n"
    "Error 3 16\n"
    "Correction \\begin{alltt}Output $\\leftarrow$ Input\\end{alltt}"
)

# Bug B: Lucas Q9d student answer (same yaml, different student)
LUCAS_Q9D = (
    "\\begin{alltt}\n"
    "SELECT species\n"
    "FROM pheasant list\n"
    "WHERE Breeding = Yes , Young $\\neq$ 0\n"
    "\\end{alltt}"
)

PREAMBLE = r"""\documentclass[10pt]{article}
\usepackage{fontspec}
\usepackage{amsmath}
\usepackage{amssymb}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage{adjustbox}
\usepackage{geometry}
\usepackage{xcolor}
\usepackage{array}
\usepackage{alltt}
\usepackage{enumitem}
\usepackage{graphicx}
\setlist[itemize]{topsep=0pt,partopsep=0pt,parsep=0pt,itemsep=2pt,leftmargin=1.2em}
\newlength{\xanswerlinegap}
\setlength{\xanswerlinegap}{0.4em}
\geometry{a4paper, margin=2cm}
\begin{document}
\renewcommand{\arraystretch}{1.6}
"""


def render(label: str, body: str) -> str:
    """Wrap a formatted cell body in a longtable matching the report layout."""
    return (
        f"\\section*{{{label}}}\n"
        "\\begin{longtable}{p{4.7cm}}\n"
        f"{body} \\\\\n"
        "\\end{longtable}\n"
    )


def run_case(label: str, raw: str, cell_w: float) -> tuple[bool, str]:
    """Format *raw* via _ai_cell, wrap in minimal doc, run xelatex.

    Returns (success, log_excerpt). success = exit 0 and no `! ` lines."""
    formatted = _ai_cell(raw, cell_width_cm=cell_w)
    tex = PREAMBLE + render(label, formatted) + "\\end{document}\n"

    workdir = Path(tempfile.mkdtemp(prefix="alltt_test_"))
    try:
        tex_path = workdir / "doc.tex"
        tex_path.write_text(tex, encoding="utf-8")
        proc = subprocess.run(
            ["xelatex", "-interaction=nonstopmode", "-halt-on-error", "doc.tex"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=60,
        )
        log = (workdir / "doc.log").read_text(encoding="utf-8", errors="replace")
        errors = re.findall(r"^!\s.*", log, flags=re.MULTILINE)
        ok = proc.returncode == 0 and not errors
        excerpt = "\n".join(errors[:3]) if errors else "(no ! lines)"
        return ok, excerpt
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    if shutil.which("xelatex") is None:
        print("SKIP: xelatex not on PATH", file=sys.stderr)
        return 0

    cases = [
        ("Andy Q5a (Bug A)", ANDY_Q5A, 4.7),
        ("Lucas Q9d (Bug B)", LUCAS_Q9D, 4.7),
    ]
    failures = []
    for label, raw, w in cases:
        ok, excerpt = run_case(label, raw, w)
        marker = "PASS" if ok else "FAIL"
        print(f"[{marker}] {label}: {excerpt}")
        if not ok:
            failures.append(label)

    if failures:
        print(f"\n{len(failures)} case(s) failed: {', '.join(failures)}")
        return 1
    print("\nAll cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
