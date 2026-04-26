"""LaTeX formatting helpers and serializers for student and class reports.

LaTeX skeletons live as Jinja2 templates in ``./templates/``. This module
prepares every dynamic value as a Python string (rows, header substrings,
geometry/column-spec lines) and then substitutes them into the template via
``<<var>>`` placeholders. No control flow happens inside the templates —
that keeps Jinja's whitespace handling out of the loop and makes byte-diffs
against the original f-string serializers easy to reason about.

Jinja delimiters here are ``<< >>`` for variables, ``<% %>`` for blocks,
``<# #>`` for comments — chosen to avoid clashing with LaTeX braces.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import jinja2


_LATEX_MAP = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "<": r"\textless{}",
    ">": r"\textgreater{}",
}
_LATEX_RE = re.compile("|".join(re.escape(k) for k in _LATEX_MAP))


def _latex_escape(text: str) -> str:
    """Escape special LaTeX characters (single-pass to avoid double-escaping)."""
    return _LATEX_RE.sub(lambda m: _LATEX_MAP[m.group()], text)


_TABULAR_RE = re.compile(r"(\\begin\{tabular\*?\}.*?\\end\{tabular\*?\})", re.DOTALL)


def _escape_bare_amp_outside_tabular(text: str) -> str:
    """Escape bare ``&`` to ``\\&`` everywhere except inside
    ``\\begin{tabular}…\\end{tabular}``, where ``&`` is the column separator."""
    parts = _TABULAR_RE.split(text)
    out = []
    for seg in parts:
        if seg.startswith(r"\begin{tabular"):
            out.append(seg)
        else:
            out.append(re.sub(r"(?<!\\)&", r"\\&", seg))
    return "".join(out)


_ALLTT_BLOCK_RE = re.compile(r"\\begin\{alltt\}.*?\\end\{alltt\}", re.DOTALL)
_ALLTT_PLACEHOLDER_RE = re.compile(r"\x00ALLTT(\d+)\x00")


# AI sometimes emits LaTeX math syntax outside ``$...$`` despite the prompt
# warnings. xelatex then errors ("Missing \cr inserted", "Missing $ inserted",
# "Undefined control sequence") and crashes the whole student report.
# ``_wrap_loose_math`` is the render-time safety net: it finds maximal "math
# runs" outside existing ``$...$`` regions and, if a run contains a math
# indicator (``^``, ``_``, or a known math-only command), wraps it in dollars.
#
# Text-mode commands like ``\textbf{...}``, ``\newline``, ``\begin{itemize}`` are
# stashed first so they cannot extend a math run or have their brace arguments
# misread as standalone math.
_TEXT_CMDS = (
    r"newline|begin|end|item|textbf|textit|texttt|textsf|textrm"
    r"|emph|hline|cr|noindent|par|textcolor|textbullet|textbackslash"
    r"|textasciicircum|textasciitilde|textless|textgreater"
)
_MATH_CMDS = (
    r"alpha|beta|gamma|delta|epsilon|zeta|eta|theta|iota|kappa"
    r"|lambda|mu|nu|xi|omicron|pi|rho|sigma|tau|upsilon|phi|chi|psi|omega"
    r"|varepsilon|vartheta|varphi|varpi|varrho|varsigma"
    r"|Alpha|Beta|Gamma|Delta|Epsilon|Zeta|Eta|Theta|Iota|Kappa"
    r"|Lambda|Mu|Nu|Xi|Omicron|Pi|Rho|Sigma|Tau|Upsilon|Phi|Chi|Psi|Omega"
    r"|times|cdot|div|pm|mp|neq|leq|geq|approx|equiv|propto|sim|simeq|cong"
    r"|rightarrow|leftarrow|to|mapsto|Rightarrow|Leftarrow"
    r"|Leftrightarrow|leftrightarrow"
    r"|sin|cos|tan|cot|sec|csc|sinh|cosh|tanh|log|ln|exp"
    r"|sqrt|frac|dfrac|tfrac|sum|prod|int|oint|lim|max|min|inf|sup|binom"
    r"|infty|emptyset|in|notin|subset|supset|cup|cap"
    r"|forall|exists|nabla|partial"
    r"|angle|perp|parallel|circ|deg|prime|dagger|degree"
    r"|text|mathrm|mathbf|mathit|mathcal|mathbb|mathsf|mathtt|operatorname"
    r"|quad|qquad"
)
# Brace group, up to 2 levels of nesting (handles `\frac{a}{b}`, `^{x_{1}}`).
_BRACE = r"\{(?:[^{}]|\{[^{}]*\})*\}"

_TEXT_CMD_RE = re.compile(rf"\\(?:{_TEXT_CMDS})\b(?:{_BRACE})*")
_DOLLAR_SPLIT_RE = re.compile(r"((?<!\\)\$[^$\n]*(?<!\\)\$)")
_MATH_RUN_RE = re.compile(
    rf"""
    (?:
        \\[A-Za-z]+(?:{_BRACE})*    # \cmd{{args}} (text-mode ones already stashed)
      | [\^_]{_BRACE}               # ^{{x}} or _{{x}}
      | [\^_][A-Za-z0-9]            # ^x or _x (single char)
      | {_BRACE}                    # bare brace group
      | [A-Za-z0-9+\-*/=().,]       # alphanum / operators
    )+
    """,
    re.VERBOSE,
)
_MATH_INDICATOR_RE = re.compile(rf"[\^_]|\\(?:{_MATH_CMDS})\b")
_STASH_RE = re.compile(r"\x00TXT(\d+)\x00")


def _wrap_loose_math(text: str) -> str:
    stashed: list[str] = []

    def _stash(m: re.Match) -> str:
        stashed.append(m.group(0))
        return f"\x00TXT{len(stashed) - 1}\x00"

    text = _TEXT_CMD_RE.sub(_stash, text)
    parts = _DOLLAR_SPLIT_RE.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 0:
            parts[i] = _MATH_RUN_RE.sub(_maybe_wrap_math, part)
    text = "".join(parts)
    return _STASH_RE.sub(lambda m: stashed[int(m.group(1))], text)


def _maybe_wrap_math(m: re.Match) -> str:
    run = m.group(0)
    if _MATH_INDICATOR_RE.search(run):
        return f"${run.rstrip()}$"
    return run


def _protect_alltt(text: str, transform) -> str:
    """Run *transform* on parts of *text* outside ``\\begin{alltt}…\\end{alltt}``.

    Inside alltt only a leading newline immediately after ``\\begin{alltt}`` and
    trailing whitespace immediately before ``\\end{alltt}`` are trimmed (so the
    block doesn't render with a blank first line); literal newlines, indentation,
    and bare ``&``/``%``/``<``/``>`` inside the block are preserved exactly.
    """
    stashed: list[str] = []

    def _stash(m: re.Match) -> str:
        stashed.append(m.group(0))
        return f"\x00ALLTT{len(stashed) - 1}\x00"

    text = _ALLTT_BLOCK_RE.sub(_stash, text)
    text = transform(text)

    def _restore(m: re.Match) -> str:
        block = stashed[int(m.group(1))]
        block = re.sub(r"(\\begin\{alltt\})\n", r"\1", block, count=1)
        block = re.sub(r"\s*(\\end\{alltt\})", r"\1", block, count=1)
        return block

    return _ALLTT_PLACEHOLDER_RE.sub(_restore, text)


def _ai_cell(text: str) -> str:
    """Prepare AI-generated LaTeX text for a p{} table cell.

    XML element text is stored verbatim (no JSON escaping layer), so no
    control-character restoration is needed.  Literal newlines in the text
    are converted to LaTeX line breaks.

    ``\\newline`` immediately before or after a block-level environment
    (``\\begin{...}`` / ``\\end{...}``) is invalid LaTeX and causes
    "There's no line here to end"; strip those.

    ``\\begin{alltt}…\\end{alltt}`` blocks are passed through unchanged
    (literal newlines, indentation, and bare ``&``/``%``/``<``/``>`` inside
    the block are preserved) so pseudocode keeps its source layout.
    """
    def _outside_alltt(t: str) -> str:
        # Defensive escape for characters AIs commonly miss when they aren't
        # legitimately part of LaTeX commands. Math ($, \, {, }) is left alone
        # so the AI can still emit `\frac{1}{2}` etc.
        t = re.sub(r"(?<!\\)%", r"\\%", t)         # bare % starts a LaTeX comment
        # Bare `\\` inside a p{} cell terminates the longtable row and shifts
        # subsequent content into the wrong columns. AIs sometimes emit `\\`
        # to mean a line break — convert it to one.
        t = re.sub(r"\\\\\s*", "\n", t)
        t = _escape_bare_amp_outside_tabular(t)    # preserve & inside \begin{tabular}
        t = _wrap_loose_math(t)                    # must precede \n → \newline
        t = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", t)
        t = t.replace("\n", "\\newline ")
        # \newline adjacent to block-level environments is invalid LaTeX
        # ("There's no line here to end") — strip it in all four positions.
        t = re.sub(r"\\newline\s*(?=\\begin\{)", "", t)
        t = re.sub(r"(?<=\})\\newline\s*(?=\\begin\{)", "", t)
        t = re.sub(r"(\\begin\{[^}]+\})\s*\\newline\b", r"\1", t)
        t = re.sub(r"(\\end\{[^}]+\})\s*\\newline\b", r"\1 ", t)
        t = re.sub(r"\\newline\s*(?=\\item\b)", "", t)
        t = re.sub(r"\\newline\s*(?=\\end\{)", "", t)
        return t

    return _protect_alltt(text, _outside_alltt)


def _format_criteria_cell(raw: str) -> str:
    """Format a marking_criteria string for the Expected column.

    Single-token criteria (one word or one number, no spaces) are grouped
    on one line joined with ' / '. Multi-word criteria each get their own line.

    ``\\begin{alltt}…\\end{alltt}`` blocks are stashed before the strip-and-group
    pass so their internal indentation survives — otherwise ``line.strip()``
    would eat the leading whitespace on every code line.
    """
    stashed: list[str] = []

    def _stash(m: re.Match) -> str:
        stashed.append(m.group(0))
        return f"\x00ALLTT{len(stashed) - 1}\x00"

    text = _ALLTT_BLOCK_RE.sub(_stash, raw)

    lines = []
    for line in text.split("\n"):
        line = re.sub(r"^\s*\[[^\]]*\]\s*", "", line).strip()
        if line:
            lines.append(line)

    if not lines:
        return "---"

    segments: list[str] = []
    short_group: list[str] = []
    for criterion in lines:
        # single token: one word or one number (not a LaTeX command, not a stashed alltt placeholder)
        is_short = (
            " " not in criterion
            and not criterion.startswith("\\")
            and not criterion.startswith("\x00")
        )
        if is_short:
            short_group.append(criterion)
        else:
            if short_group:
                segments.append(" / ".join(short_group))
                short_group = []
            segments.append(criterion)
    if short_group:
        segments.append(" / ".join(short_group))

    result = "\n".join(segments)
    result = _ALLTT_PLACEHOLDER_RE.sub(
        lambda m: stashed[int(m.group(1))], result
    )
    return _ai_cell(result)


def _awarded_tex(awarded: int | None, max_q: int | str) -> str:
    """Render awarded marks with colour: green=full, red=zero, plain=partial."""
    if awarded is None:
        return "\\textit{?}"
    if awarded == 0:
        return f"\\textcolor{{red!65!black}}{{{awarded}}}"
    try:
        if int(awarded) == int(max_q):
            return f"\\textcolor{{green!55!black}}{{{awarded}}}"
    except (TypeError, ValueError):
        pass
    return str(awarded)


# ---------------------------------------------------------------------------
# Jinja2 environment — non-default delimiters to avoid LaTeX brace clashes.
# ``keep_trailing_newline=True`` preserves the trailing newline of each
# template file so output matches the original f-string serializers exactly.
# ---------------------------------------------------------------------------

_ENV = jinja2.Environment(
    block_start_string="<%",   block_end_string="%>",
    variable_start_string="<<", variable_end_string=">>",
    comment_start_string="<#", comment_end_string="#>",
    loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"),
    keep_trailing_newline=True,
    autoescape=False,
)


def _student_report_to_tex(
    report: dict,
    exam_name: str = "",
    orientation: str = "landscape",
    font_size: int = 10,
    show_curved_grade: bool = True,
) -> str:
    name = _latex_escape(report["student_name"])
    total = report["total_marks"]
    max_m = report["max_marks"]
    pct = report["percentage"]
    date_str = datetime.date.today().isoformat()
    header_extra = f" — {_latex_escape(exam_name.replace('_', ' '))}" if exam_name else ""
    rows = []
    for q in report["questions"]:
        qnum = _latex_escape(str(q.get("number", "")).replace("_", "."))
        max_q = q.get("max_marks", "")
        awarded = q.get("assigned_marks")
        answer_raw = str(q.get("student_answer") or "").strip()
        answer = (
            "\\textit{(blank)}" if not answer_raw
            else _ai_cell(answer_raw)
        )
        correct_raw = str(q.get("correct_answer") or "").strip()
        criteria_raw = str(q.get("marking_criteria") or "").strip()
        question_type = str(q.get("question_type", "")).strip()
        if question_type == "multiple_choice" or not criteria_raw:
            # MCQ: always show the answer letter.
            # Non-MCQ without criteria: fall back to correct_answer.
            correct_ans = _ai_cell(correct_raw) if correct_raw else "---"
        else:
            # Non-MCQ with criteria: show the full breakdown regardless of correct_answer.
            correct_ans = _format_criteria_cell(criteria_raw)
        reasoning = _ai_cell(str(q.get("explanation") or ""))
        awarded_cell = _awarded_tex(awarded, max_q)
        rows.append(
            f"    {qnum} & {max_q} & {awarded_cell} & {answer} & {correct_ans} & {reasoning} \\\\ \\hline"
        )
    rows_str = "\n".join(rows)
    curved_pct = report.get("curved_pct")
    pct_display = "N/A" if pct is None else f"{pct}\\%"
    curved_display = "N/A" if curved_pct is None else f"{curved_pct}\\%"
    # Header summary: "X% raw, Y% curved" by default, or just "X%" when the
    # curved grade is hidden (env CURVED_GRADE_VISIBLE=false or prompt
    # "hide curve from students").
    summary_text = (
        f"{pct_display} raw, {curved_display} curved"
        if show_curved_grade
        else pct_display
    )
    # Column widths fill the available text width minus ~2.5 cm of
    # \tabcolsep separator overhead across 6 columns:
    #   landscape A4 (25.7 cm text - 2.5 cm overhead) → ~22.7 cm column budget
    #     = p{0.6} + p{0.6} + p{0.7} + p{5.7} + p{7.0} + p{8.1}
    #   portrait  A4 (19.0 cm text - 2.5 cm overhead) → ~16.5 cm column budget
    #     = p{0.4} + p{0.4} + p{0.5} + p{3.6} + p{5.0} + p{5.5}
    # Portrait widths × (16.5 / 22.7) ≈ 0.727 of landscape widths.
    if orientation == "portrait":
        geometry_line = "\\geometry{a4paper,margin=1cm,footskip=6pt}\n"
        col_spec = "L{0.4cm}L{0.4cm}L{0.5cm}L{3.6cm}L{5cm}L{5.5cm}"
    else:
        geometry_line = "\\geometry{a4paper,landscape,margin=2cm}\n"
        col_spec = "L{0.6cm}L{0.6cm}L{0.7cm}L{5.7cm}L{7.0cm}L{8.1cm}"
    table_open  = "{\\small\n" if font_size < 12 else ""
    table_close = "}\n"        if font_size < 12 else ""
    return _ENV.get_template("student_report.tex.j2").render(
        font_size=font_size,
        geometry_line=geometry_line,
        name=name,
        header_extra=header_extra,
        total=total,
        max_m=max_m,
        summary_text=summary_text,
        date_str=date_str,
        table_open=table_open,
        col_spec=col_spec,
        rows_str=rows_str,
        table_close=table_close,
    )


def _class_report_to_tex(report: dict, exam_name: str = "") -> str:
    header_extra = f" — {_latex_escape(exam_name.replace('_', ' '))}" if exam_name else ""
    date_str = datetime.date.today().isoformat()
    student_rows = []
    for s in report["students"]:
        name = _latex_escape(s["name"])
        pct_display = "N/A" if s["percentage"] is None else f"{s['percentage']}\\%"
        curved_display = "N/A" if s.get("curved_pct") is None else f"{s['curved_pct']}\\%"
        rank_cell = str(s["rank"]) if s.get("rank") is not None else "---"
        student_rows.append(f"    {rank_cell} & {name} & {s['total_marks']} & {pct_display} & {curved_display} \\\\")
    student_rows_str = "\n".join(student_rows)

    q_max = report.get("per_question_max_marks", {})
    q_pct = report.get("per_question_pct_averages", {})
    q_rows = []
    for qnum, avg in sorted(
        report.get("per_question_averages", {}).items(),
        key=lambda x: (q_pct.get(x[0], float("inf")), x[0]),
    ):
        max_cell = str(q_max.get(qnum, "")) if q_max else ""
        pct_cell = f"{q_pct[qnum]}\\%" if qnum in q_pct else "N/A"
        q_rows.append(
            f"    {_latex_escape(qnum.replace('_', '.'))} & {max_cell} & {avg} & {pct_cell} \\\\"
        )
    q_rows_str = "\n".join(q_rows)

    class_avg_display = (
        "N/A" if report["class_average_pct"] is None
        else f"{report['class_average_pct']}\\%"
    )

    return _ENV.get_template("class_report.tex.j2").render(
        header_extra=header_extra,
        class_avg_display=class_avg_display,
        total_max_marks=report["total_max_marks"],
        date_str=date_str,
        student_rows_str=student_rows_str,
        q_rows_str=q_rows_str,
        histogram_path=report.get("histogram_path"),
        difficulty_path=report.get("difficulty_path"),
    )
