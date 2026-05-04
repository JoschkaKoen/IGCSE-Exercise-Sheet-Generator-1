"""LaTeX formatting helpers and serializers for student and class reports.

LaTeX skeletons live as Jinja2 templates in ``./templates/``. This module
prepares every dynamic value as a Python string (rows, header substrings,
geometry/column-spec lines) and then substitutes them into the template via
``<<var>>`` placeholders. No control flow happens inside the templates —
that keeps Jinja's whitespace handling out of the loop and makes byte-diffs
against the original f-string serializers easy to reason about.

Jinja delimiters here are ``<< >>`` for variables, ``<% %>`` for blocks,
``<# #>`` for comments — chosen to avoid clashing with LaTeX braces.

Text-manipulation primitives (escape, math wrap, alltt protect, bullet
conversion) live in :mod:`report_latex_text`. AI-cell formatters and
oversized-cell splitting live in :mod:`report_latex_cells`. This module
imports from both and renders the final TeX via Jinja.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path

import jinja2
import yaml

from xscore.marking.report_latex_cells import (
    _ai_cell,
    _awarded_tex,
    _format_criteria_cell,
    _split_oversized_cell,
)
from xscore.marking.report_latex_text import _latex_escape
from xscore.scaffold.scaffold_qtree import _norm_qnum
from xscore.shared.path_builders import (
    artifact_mark_scheme_graphics_dir,
    artifact_mark_scheme_graphics_yaml_path,
)


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


# Step 22 writes PNG filenames via `[^\w] -> _` (defensive against unsafe
# chars in raw qnums) but uses `_norm_qnum` (parens stripped) for the
# canonical question number everywhere else. The marking pipeline's
# `q["number"]` is the canonical form (e.g. `"7a"` for a leaf `"7(a)"`), so
# `_scheme_graphics_by_qnum` keys its dict by `_norm_qnum(raw)` for lookup
# parity and globs files using the original `[^\w]->_` transform.
_QNUM_SAFE_RE = re.compile(r"[^\w]")


def _scheme_graphics_safe_qnum(qnum: str) -> str:
    """Canonical key into the dict from ``_scheme_graphics_by_qnum``."""
    return _norm_qnum(str(qnum))


def _scheme_graphics_by_qnum(artifact_dir: Path) -> dict[str, list[str]]:
    """Map canonical qnum (e.g. ``"7(a)" -> "7a"`` via ``_norm_qnum``) ->
    list of PNG filenames step 22 extracted for that question.

    Sources raw qnums from ``mark_scheme_graphics.yaml`` so dict keys match
    the marking pipeline's canonical ``q["number"]`` form. Files are matched
    by the same ``[^\\w]->_`` transform that
    ``scaffold_xml._extract_scheme_graphics`` applied when writing them.
    """
    out: dict[str, list[str]] = {}
    graphics_dir = artifact_mark_scheme_graphics_dir(artifact_dir)
    yaml_path = artifact_mark_scheme_graphics_yaml_path(artifact_dir)
    if not graphics_dir.is_dir() or not yaml_path.is_file():
        return out
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return out
    if not isinstance(data, dict):
        return out
    for q in data.get("questions", []):
        raw = str((q.get("number") or "")).strip()
        if not raw or not q.get("graphics"):
            continue
        key = _norm_qnum(raw)
        safe = _QNUM_SAFE_RE.sub("_", raw)
        for pf in sorted(graphics_dir.glob(f"*_{safe}_*.png")):
            out.setdefault(key, []).append(pf.name)
    return out


def _scheme_graphics_tex(filenames: list[str]) -> str:
    """Build the LaTeX fragment that embeds extracted scheme graphics in the
    Expected column of a per-student report. Appended to the criteria cell
    after a single space — the preceding `\\end{itemize}` already returns
    the parbox to vertical mode, so `\\includegraphics` starts a new line on
    its own. (`\\newline` after `\\end{...}` would raise "no line here to end".)"""
    return " ".join(
        rf"\includegraphics[width=\linewidth]{{{f}}}" for f in filenames
    )


def _student_header_kwargs(
    report: dict,
    exam_name: str,
    show_curved_grade: bool,
    class_avg: int | None,
    subtitle: str | None = None,
) -> dict:
    """Build the Jinja2 kwargs shared by all three per-student-report templates.

    *subtitle* (when set) is appended to ``header_extra`` so the title line
    distinguishes the ``_attempted`` variants from the canonical ones —
    avoids touching the templates and preserves byte-identity when None.
    """
    pct = report["percentage"]
    curved_pct = report.get("curved_pct")
    pct_display = "N/A" if pct is None else f"{pct}\\%"
    curved_display = "N/A" if curved_pct is None else f"{round(curved_pct)}\\%"
    header_extra = (
        f" — {_latex_escape(exam_name.replace('_', ' '))}" if exam_name else ""
    )
    if subtitle:
        header_extra += f" — {_latex_escape(subtitle)}"
    return {
        "name": _latex_escape(report["student_name"]),
        "header_extra": header_extra,
        "total": report["total_marks"],
        "max_m": report["max_marks"],
        "percentage": pct,
        "class_avg_pct": class_avg,
        "summary_text": (
            f"{pct_display} raw, {curved_display} curved"
            if show_curved_grade else pct_display
        ),
        "date_str": datetime.date.today().isoformat(),
    }


def _student_report_to_tex(
    report: dict,
    exam_name: str = "",
    orientation: str = "landscape",
    font_size: int = 10,
    show_curved_grade: bool = True,
    class_avg: int | None = None,
    q_to_graphics: dict[str, list[str]] | None = None,
    scheme_graphics_dir: str = "",
    subtitle: str | None = None,
) -> str:
    # Column widths threaded into _ai_cell / _format_criteria_cell so alltt
    # font-size selection scales with cell width. Match the col_spec below.
    # `panel_budget` is forwarded to `_split_oversized_cell` so very tall
    # Expected cells are emitted as multi-row panels (avoids cell overflow
    # off-page; longtable allows page breaks between rows but not within).
    if orientation == "portrait":
        ans_w, exp_w, reason_w = 3.6, 5.0, 5.5
        panel_budget = 40.0
    else:
        ans_w, exp_w, reason_w = 5.7, 7.0, 8.1
        panel_budget = 22.0
    q_to_graphics = q_to_graphics or {}
    rows = []
    for q in report["questions"]:
        qnum = _latex_escape(str((q.get("number") or "")).replace("_", "."))
        max_q = q.get("max_marks", "")
        awarded = q.get("assigned_marks")
        answer_raw = str(q.get("student_answer") or "").strip()
        if q.get("_unanswered"):
            answer = "\\textit{(not answered)}"
        elif not answer_raw:
            answer = "\\textit{(blank)}"
        else:
            answer = _ai_cell(answer_raw, ans_w)
        correct_raw = str(q.get("correct_answer") or "").strip()
        criteria_raw = str(q.get("marking_criteria") or "").strip()
        question_type = str(q.get("question_type", "")).strip()
        if question_type == "multiple_choice" or not criteria_raw:
            # MCQ: always show the answer letter.
            # Non-MCQ without criteria: fall back to correct_answer.
            correct_ans = _ai_cell(correct_raw, exp_w) if correct_raw else "---"
        else:
            # Non-MCQ with criteria: show the full breakdown regardless of correct_answer.
            correct_ans = _format_criteria_cell(criteria_raw, exp_w)
        gfx_files = q_to_graphics.get(_scheme_graphics_safe_qnum((q.get("number") or "")), [])
        if gfx_files:
            correct_ans = correct_ans + " " + _scheme_graphics_tex(gfx_files)
        reasoning = _ai_cell(str(q.get("explanation") or ""), reason_w)
        awarded_cell = _awarded_tex(awarded, max_q)
        panels = _split_oversized_cell(correct_ans, panel_budget)
        if len(panels) == 1:
            rows.append(
                f"    {qnum} & {max_q} & {awarded_cell} & {answer} & {panels[0]} & {reasoning} \\\\ \\hline"
            )
        else:
            # Q12-style mega Expected: emit one row per panel; continuation
            # rows have empty Q/Max/Got/Answer/Reasoning so panels appear as
            # one logical row. Only the final continuation row carries
            # \hline (separates from the next question).
            rows.append(
                f"    {qnum} & {max_q} & {awarded_cell} & {answer} & {panels[0]} & {reasoning} \\\\"
            )
            for i, panel in enumerate(panels[1:], 1):
                terminator = "\\\\ \\hline" if i == len(panels) - 1 else "\\\\"
                rows.append(f"     &  &  &  & {panel} &  {terminator}")
    if not rows:
        rows.append(
            "    \\multicolumn{6}{c}{\\textit{(no answers extracted)}} \\\\"
        )
    rows_str = "\n".join(rows)
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
        table_open=table_open,
        col_spec=col_spec,
        rows_str=rows_str,
        table_close=table_close,
        scheme_graphics_dir=scheme_graphics_dir,
        **_student_header_kwargs(report, exam_name, show_curved_grade, class_avg, subtitle),
    )


def _format_q_rows(avgs: dict, q_max: dict, q_pct: dict) -> str:
    """Build LaTeX rows for a question-ranking table, sorted hardest first."""
    rows = []
    for qnum, avg in sorted(
        avgs.items(),
        key=lambda x: (q_pct.get(x[0], float("inf")), x[0]),
    ):
        max_cell = str(q_max.get(qnum, "")) if q_max else ""
        pct_cell = f"{q_pct[qnum]}\\%" if qnum in q_pct else "N/A"
        rows.append(
            f"    {_latex_escape(qnum.replace('_', '.'))} & {max_cell} & {avg} & {pct_cell} \\\\"
        )
    return "\n".join(rows)


def _class_report_to_tex(report: dict, exam_name: str = "") -> str:
    header_extra = f" — {_latex_escape(exam_name.replace('_', ' '))}" if exam_name else ""
    date_str = datetime.date.today().isoformat()
    student_rows = []
    for s in report["students"]:
        name = _latex_escape(s["name"])
        pct_display = "N/A" if s["percentage"] is None else f"{s['percentage']}\\%"
        curved_display = "N/A" if s.get("curved_pct") is None else f"{round(s['curved_pct'])}\\%"
        rank_cell = str(s["rank"]) if s.get("rank") is not None else "---"
        student_rows.append(f"    {rank_cell} & {name} & {s['total_marks']} & {pct_display} & {curved_display} \\\\")
    student_rows_str = "\n".join(student_rows)

    q_max = report.get("per_question_max_marks", {})
    q_pct = report.get("per_question_pct_averages", {})
    q_rows_str = _format_q_rows(report.get("per_question_averages", {}), q_max, q_pct)

    top_q_rows_str = _format_q_rows(
        report.get("per_top_question_averages", {}),
        report.get("per_top_question_max_marks", {}),
        report.get("per_top_question_pct_averages", {}),
    )

    class_avg_display = (
        "N/A" if report["class_average_pct"] is None
        else f"{report['class_average_pct']}\\%"
    )

    return _ENV.get_template("class_report.tex.j2").render(
        header_extra=header_extra,
        class_avg_display=class_avg_display,
        total_max_marks=report["total_max_marks"],
        n_students=report.get("n_students"),
        median_pct=report.get("median_pct"),
        min_pct=report.get("min_pct"),
        max_pct=report.get("max_pct"),
        date_str=date_str,
        student_rows_str=student_rows_str,
        q_rows_str=q_rows_str,
        top_q_rows_str=top_q_rows_str,
        histogram_raw_path=report.get("histogram_raw_path"),
        histogram_curved_path=report.get("histogram_curved_path"),
        difficulty_path=report.get("difficulty_path"),
        difficulty_top_path=report.get("difficulty_top_path"),
    )


def _class_toc_to_tex(
    students: list[dict], exam_name: str = "", title_suffix: str = ""
) -> str:
    """TOC for the combined class report — one clickable line per student.

    *students* is a list of dicts with ``safe_name``, ``display_name``,
    and ``page`` keys. Display names are pre-escaped here so the
    template stays a plain substitution.

    *title_suffix* lets callers distinguish per-variant TOCs in the
    section heading (e.g. ``" (landscape)"``); empty by default.
    """
    header_extra = f" — {_latex_escape(exam_name.replace('_', ' '))}" if exam_name else ""
    header = f"Students{header_extra}{title_suffix}"
    rendered = [
        {
            "safe_name": s["safe_name"],
            "display_name": _latex_escape(s["display_name"]),
            "page": s["page"],
        }
        for s in students
    ]
    return _ENV.get_template("class_toc.tex.j2").render(
        header=header, students=rendered
    )


# ---------------------------------------------------------------------------
# Parsed-exam question rendering (step 29: exam_questions.pdf,
# *_landscape_with_questions.pdf, *_portrait_list.pdf).
# ---------------------------------------------------------------------------

def _build_question_index(parsed_questions: list[dict]) -> dict[str, dict]:
    """DFS the parsed-exam tree; map every node's bare number to its dict."""
    index: dict[str, dict] = {}

    def _visit(q: dict) -> None:
        num = str((q.get("number") or "")).strip()
        if num:
            index[num] = q
        for sub in q.get("subquestions") or []:
            _visit(sub)

    for q in parsed_questions or []:
        _visit(q)
    return index


def _question_text_for_row(qnum: str, qmap: dict[str, dict]) -> dict | None:
    """Look up a parsed-exam question by row number, stripping ``_2``-style suffix."""
    base = str(qnum).partition("_")[0]
    return qmap.get(base)


def _render_question_with_options(q: dict | None, cell_width_cm: float = 3.6) -> str:
    """Render question stem plus an ``(A)/(B)/...`` itemize for MCQs.

    Returns empty string for parent-only nodes (text == "" and no options) so
    the recursive ``_question_to_tex`` can omit a blank body line for them.
    """
    if not q:
        return r"\textit{(text unavailable)}"
    parts: list[str] = []
    text = str(q.get("text") or "").strip()
    if text:
        parts.append(_ai_cell(text, cell_width_cm))
    if str(q.get("type") or "") == "multiple_choice":
        opts = q.get("answer_options") or q.get("options") or []
        if opts:
            items: list[str] = []
            for opt in opts:
                letter = _latex_escape(str(opt.get("letter") or "").strip())
                opt_text = _ai_cell(str(opt.get("text") or "").strip(), cell_width_cm)
                items.append(f"  \\item[({letter})] {opt_text}")
            parts.append(
                "\\begin{itemize}[leftmargin=2em,itemsep=0pt]\n"
                + "\n".join(items)
                + "\n\\end{itemize}"
            )
    # Visible gap between stem text and MCQ options. `\vspace*` (starred) is
    # non-discardable; bare `\smallskip` glue before `\begin{itemize}` is
    # absorbed by the list's topsep handling and renders as zero space.
    return "\n\\par\\vspace*{0.5em}\n".join(parts)


def _question_to_tex(q: dict, depth: int = 0) -> str:
    """Recursive renderer used by ``exam_questions.pdf``.

    Top-level questions render flush-left; subquestions are indented with
    ``\\setlength{\\leftskip}{...em}`` inside a TeX group so wrapped lines stay
    aligned without pulling in ``changepage``.
    """
    num = _latex_escape(str((q.get("number") or "")))
    marks = q.get("marks") or 0
    marks_label = ""
    if marks:
        marks_label = f" \\hfill {marks} mark{'s' if marks != 1 else ''}"
    # exam_questions.pdf is A4 with 1.5cm margins → 18cm text width. Each
    # subquestion depth indents 1.5em (~0.5cm at 11pt body); subtract that
    # so deeply-nested alltt sizes correctly. Floor at 4cm to avoid silly
    # widths if the recursion ever goes very deep.
    block_w = max(4.0, 18.0 - depth * 0.5)
    body = _render_question_with_options(q, block_w)

    lines = [f"\\noindent\\textbf{{Q{num}}}{marks_label}\\par\\nopagebreak"]
    if body:
        lines.append(f"{body}\\par")
    block = "\n".join(lines)
    if depth > 0:
        block = f"{{\\setlength{{\\leftskip}}{{{depth * 1.5}em}}\n{block}\n}}"

    subs = q.get("subquestions") or []
    if subs:
        sub_blocks = "\n\\smallskip\n".join(
            _question_to_tex(sub, depth + 1) for sub in subs
        )
        block = f"{block}\n\\smallskip\n{sub_blocks}"
    return block


def _exam_questions_to_tex(parsed_questions: list[dict], exam_name: str = "") -> str:
    """Render all parsed exam questions into a standalone TeX document."""
    header_extra = f" — {_latex_escape(exam_name.replace('_', ' '))}" if exam_name else ""
    date_str = datetime.date.today().isoformat()
    body_blocks = [_question_to_tex(q) for q in parsed_questions or []]
    body = "\n\\vspace{1em}\n".join(body_blocks)
    return _ENV.get_template("exam_questions.tex.j2").render(
        header_extra=header_extra,
        date_str=date_str,
        body=body,
    )


def _student_report_with_questions_to_tex(
    report: dict,
    qmap: dict[str, dict],
    exam_name: str = "",
    font_size: int = 10,
    show_curved_grade: bool = True,
    class_avg: int | None = None,
    q_to_graphics: dict[str, list[str]] | None = None,
    scheme_graphics_dir: str = "",
    subtitle: str | None = None,
) -> str:
    """Landscape per-student PDF with an extra Question column (no MCQ options)."""
    # Column widths threaded into _ai_cell / _format_criteria_cell /
    # _render_question_with_options so alltt font-size selection scales with cell
    # width. Match the col_spec below.
    qstem_w, ans_w, exp_w, reason_w = 4.5, 4.7, 5.0, 6.2
    q_to_graphics = q_to_graphics or {}
    rows = []
    for q in report["questions"]:
        qnum_raw = str((q.get("number") or ""))
        qnum = _latex_escape(qnum_raw.replace("_", "."))
        max_q = q.get("max_marks", "")
        awarded = q.get("assigned_marks")
        answer_raw = str(q.get("student_answer") or "").strip()
        if q.get("_unanswered"):
            answer = "\\textit{(not answered)}"
        elif not answer_raw:
            answer = "\\textit{(blank)}"
        else:
            answer = _ai_cell(answer_raw, ans_w)
        correct_raw = str(q.get("correct_answer") or "").strip()
        criteria_raw = str(q.get("marking_criteria") or "").strip()
        question_type = str(q.get("question_type", "")).strip()
        if question_type == "multiple_choice" or not criteria_raw:
            correct_ans = _ai_cell(correct_raw, exp_w) if correct_raw else "---"
        else:
            correct_ans = _format_criteria_cell(criteria_raw, exp_w)
        gfx_files = q_to_graphics.get(_scheme_graphics_safe_qnum(qnum_raw), [])
        if gfx_files:
            correct_ans = correct_ans + " " + _scheme_graphics_tex(gfx_files)
        reasoning = _ai_cell(str(q.get("explanation") or ""), reason_w)
        awarded_cell = _awarded_tex(awarded, max_q)
        question_cell = _render_question_with_options(_question_text_for_row(qnum_raw, qmap), qstem_w)
        rows.append(
            f"    {qnum} & {question_cell} & {max_q} & {awarded_cell} & {answer} & {correct_ans} & {reasoning} \\\\ \\hline"
        )
    if not rows:
        rows.append(
            "    \\multicolumn{7}{c}{\\textit{(no answers extracted)}} \\\\"
        )
    rows_str = "\n".join(rows)
    # Landscape A4: 25.7 cm text - ~3.0 cm \tabcolsep overhead across 7 cols
    # → ~22.7 cm column budget = 0.5+4.5+0.5+0.6+4.7+5.0+6.2 (cm).
    geometry_line = "\\geometry{a4paper,landscape,margin=2cm}\n"
    col_spec = "L{0.5cm}L{4.5cm}L{0.5cm}L{0.6cm}L{4.7cm}L{5.0cm}L{6.2cm}"
    table_open  = "{\\small\n" if font_size < 12 else ""
    table_close = "}\n"        if font_size < 12 else ""
    return _ENV.get_template("student_report_with_questions.tex.j2").render(
        font_size=font_size,
        geometry_line=geometry_line,
        table_open=table_open,
        col_spec=col_spec,
        rows_str=rows_str,
        table_close=table_close,
        scheme_graphics_dir=scheme_graphics_dir,
        **_student_header_kwargs(report, exam_name, show_curved_grade, class_avg, subtitle),
    )


def _student_report_list_to_tex(
    report: dict,
    qmap: dict[str, dict],
    exam_name: str = "",
    show_curved_grade: bool = True,
    class_avg: int | None = None,
    q_to_graphics: dict[str, list[str]] | None = None,
    scheme_graphics_dir: str = "",
    subtitle: str | None = None,
) -> str:
    """Portrait per-student PDF in a list/block layout (no longtable).

    Each row of ``report["questions"]`` becomes one block: header line, question
    prompt (with MCQ options inline), then labeled paragraphs for student
    answer / expected / reasoning, separated by a thin horizontal rule.
    """
    # Block layout, no longtable: each labeled paragraph spans the full text
    # width. A4 portrait with 1.5cm margins = 21 - 3 = 18cm.
    block_w = 18.0
    q_to_graphics = q_to_graphics or {}

    blocks: list[str] = []
    for q in report["questions"]:
        qnum_raw = str((q.get("number") or ""))
        qnum_dotted = _latex_escape(qnum_raw.replace("_", "."))
        max_q = q.get("max_marks", "")
        awarded = q.get("assigned_marks")
        awarded_cell = _awarded_tex(awarded, max_q)
        answer_raw = str(q.get("student_answer") or "").strip()
        if q.get("_unanswered"):
            answer = "\\textit{(not answered)}"
        elif not answer_raw:
            answer = "\\textit{(blank)}"
        else:
            answer = _ai_cell(answer_raw, block_w)
        correct_raw = str(q.get("correct_answer") or "").strip()
        criteria_raw = str(q.get("marking_criteria") or "").strip()
        question_type = str(q.get("question_type", "")).strip()
        if question_type == "multiple_choice" or not criteria_raw:
            expected = _ai_cell(correct_raw, block_w) if correct_raw else "---"
        else:
            expected = _format_criteria_cell(criteria_raw, block_w)
        gfx_files = q_to_graphics.get(_scheme_graphics_safe_qnum(qnum_raw), [])
        if gfx_files:
            expected = expected + " " + _scheme_graphics_tex(gfx_files)
        reasoning = _ai_cell(str(q.get("explanation") or ""), block_w)
        question_body = _render_question_with_options(
            _question_text_for_row(qnum_raw, qmap), block_w
        ) or r"\textit{(text unavailable)}"
        blocks.append(
            f"\\noindent\\textbf{{Q{qnum_dotted}}} \\hfill {awarded_cell} / {max_q}\\par\n"
            f"\\smallskip\\textbf{{Question:}}\\par\n"
            f"{question_body}\\par\n"
            f"\\smallskip\\textbf{{Your answer:}}\\par\n"
            f"{answer}\\par\n"
            f"\\smallskip\\textbf{{Expected:}}\\par\n"
            f"{expected}\\par\n"
            f"\\smallskip\\textbf{{Reasoning:}}\\par\n"
            f"{reasoning}\\par\n"
            f"\\vspace{{0.4em}}\\hrule\\vspace{{0.6em}}"
        )
    if not blocks:
        blocks.append("\\noindent\\textit{(no answers extracted)}")
    body = "\n".join(blocks)

    return _ENV.get_template("student_report_list.tex.j2").render(
        body=body,
        scheme_graphics_dir=scheme_graphics_dir,
        **_student_header_kwargs(report, exam_name, show_curved_grade, class_avg, subtitle),
    )
