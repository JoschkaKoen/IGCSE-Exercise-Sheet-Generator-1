# -*- coding: utf-8 -*-
"""LaTeX document assembly for MCQ explanation sheets.

Builds the complete LaTeX source from structured explanation data.
Page geometry constants are also defined here and re-exported for
use by the compile module.
"""

from __future__ import annotations

from .config import (
    A4_HEIGHT_PT,
    A4_WIDTH_PT,
    EXAM_LABEL_FONT_PT,
    EXAM_LABEL_TOP_PT,
    OUTPUT_MARGIN_PT,
    OUTPUT_MARGIN_RIGHT_PT,
)
from .latex_utils import latex_escape as _latex_escape
from .latex_utils import sanitize_bullet as _sanitize_bullet

# ---------------------------------------------------------------------------
# Page geometry (derived from config; used here and in mcq_compile)
# ---------------------------------------------------------------------------

_MARGIN_PT = float(OUTPUT_MARGIN_PT)
_MARGIN_RIGHT_PT = float(OUTPUT_MARGIN_RIGHT_PT)
_USABLE_W_PT = A4_WIDTH_PT - _MARGIN_PT - _MARGIN_RIGHT_PT

# Available height per output page for embedded strips (mirrors rendering.py constants).
# The header band occupies: LABEL_TOP + (LABEL_FS + 8) + LABEL_GAP = 10 + 17 + 6 = 33 pt.
_LABEL_H = float(EXAM_LABEL_FONT_PT) + 8.0
_LABEL_GAP_PT = 6.0
_OUTPUT_INITIAL_Y = float(EXAM_LABEL_TOP_PT) + _LABEL_H + _LABEL_GAP_PT  # ≈ 33 pt
_USABLE_H_PT = A4_HEIGHT_PT - _MARGIN_PT - _OUTPUT_INITIAL_Y             # ≈ 799 pt


def _choose_pairs_per_row(n: int) -> int:
    """Return the number of Q/Ans pairs per row that divides *n* exactly.

    Tries values near 5 first so the table stays compact.  Falls back to 5
    (or n itself if n < 5) when no preferred divisor works.
    """
    for r in [5, 4, 6, 3, 7, 8, 10]:
        if r <= n and n % r == 0:
            return r
    return min(5, n)


def _build_answer_table(questions: list[int], answers: dict[int, str]) -> str:
    """Build a compact answer table with vertical rules between Q/Ans pairs.

    The number of pairs per row is chosen so every row is fully filled,
    eliminating any hanging separator on the last row.
    """
    rows: list[tuple[int, str]] = [(q, answers[q]) for q in questions if q in answers]
    if not rows:
        return ""

    # Pick pairs_per_row so len(rows) % pairs_per_row == 0 whenever possible.
    # >{\bfseries} requires \usepackage{array}.
    pairs_per_row = _choose_pairs_per_row(len(rows))
    pair_spec = r"r>{\bfseries}l"
    col_spec = r" | ".join([pair_spec] * pairs_per_row)
    col_arg = r"@{}" + col_spec + r"@{}"

    chunks = [rows[i:i + pairs_per_row] for i in range(0, len(rows), pairs_per_row)]

    lines = [
        r"\begin{tabular}{" + col_arg + "}",
        r"\toprule",
    ]
    for chunk in chunks:
        cells_list = [f"{q} & {a}" for q, a in chunk]
        missing = pairs_per_row - len(chunk)
        if missing:
            # Rare fallback: span unused columns as one invisible cell so LaTeX
            # does not raise a column-count mismatch error.
            cells_list.append(rf"\multicolumn{{{missing * 2}}}{{l}}{{}}")
        lines.append(" & ".join(cells_list) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines)


def build_explanation_latex(
    questions: list[int],
    answers: dict[int, str],
    explanations: dict[int, list[str]],
    paper_label: str,
    exam_key: str | None = None,
) -> str:
    """Assemble the complete LaTeX source for the MCQ explanation document."""
    table = _build_answer_table(questions, answers)

    sections: list[str] = []
    for q in questions:
        if q not in answers:
            continue
        ans = answers[q]
        bullets = explanations.get(q)
        items = ""
        non_empty = [b for b in (bullets or []) if b.strip()]
        if non_empty:
            item_lines = "\n".join(
                r"  \item " + _sanitize_bullet(b) for b in non_empty
            )
            items = f"\\begin{{itemize}}[leftmargin=1.6em, itemsep=2pt, topsep=2pt, parsep=0pt]\n{item_lines}\n\\end{{itemize}}"
        else:
            items = r"\textit{(Explanation not available.)}"

        sections.append(
            f"\\needspace{{4\\baselineskip}}\n"
            f"\\vspace{{6pt}}\n"
            f"{{\\bfseries Question {q}\\enspace{{\\normalfont\\small (Answer: \\textbf{{{ans}}})}}}}\n"
            f"\\nopagebreak\n\n"
            f"{items}"
        )

    escaped_label = _latex_escape(paper_label) if paper_label else "Multiple Choice"

    # Build the "Answers Q38–40:" side label (same bold 11pt as the Question headings below).
    answered_qs = [q for q in questions if q in answers]
    if answered_qs:
        q_min, q_max = min(answered_qs), max(answered_qs)
        q_range = f"Q{q_min}" if q_min == q_max else f"Q{q_min}--{q_max}"
        side_label = rf"\bfseries Answers {q_range}:"
    else:
        side_label = r"\bfseries Answers:"

    # \hfill TABLE \hfill\phantom{label} centres the table on the full linewidth:
    # the phantom mirrors the label's width on the right so both \hfills are equal.
    header_row = (
        rf"\noindent {{{side_label}}}"
        r"\hfill"
        "\n"
        f"{table}"
        "\n"
        rf"\hfill\phantom{{{side_label}}}"
    )

    body = "\n\n".join(sections)

    mhchem_line = r"\usepackage[version=4]{mhchem}" if exam_key == "chemistry" else ""

    return rf"""\documentclass[12pt]{{article}}
\usepackage[utf8]{{inputenc}}
\usepackage[T1]{{fontenc}}
\usepackage[a4paper, top=0cm, bottom=1.1cm, left=1.2cm, right=1.5cm]{{geometry}}
\usepackage{{amsmath, amssymb}}
{mhchem_line}
\usepackage{{array}}
\usepackage{{booktabs}}
\usepackage[shortlabels]{{enumitem}}
\usepackage{{parskip}}
\usepackage{{lmodern}}
\usepackage{{microtype}}
\usepackage{{needspace}}
\usepackage{{xcolor}}

\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{4pt}}
\setlength{{\topskip}}{{0pt}}
\pagestyle{{empty}}

\begin{{document}}

{header_row}

\vspace{{4pt}}

{body}

\end{{document}}
"""
