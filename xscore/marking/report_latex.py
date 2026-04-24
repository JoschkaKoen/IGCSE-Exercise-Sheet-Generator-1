"""LaTeX formatting helpers and serializers for student and class reports."""

from __future__ import annotations

import re


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


def _ai_cell(text: str) -> str:
    """Prepare AI-generated LaTeX text for a p{} table cell.

    XML element text is stored verbatim (no JSON escaping layer), so no
    control-character restoration is needed.  Literal newlines in the text
    are converted to LaTeX line breaks.

    ``\\newline`` immediately before or after a block-level environment
    (``\\begin{...}`` / ``\\end{...}``) is invalid LaTeX and causes
    "There's no line here to end"; strip those.
    """
    result = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", text)
    result = result.replace("\n", "\\newline ")
    # \newline adjacent to block-level environments is invalid LaTeX
    # ("There's no line here to end") — strip it in all four positions.
    result = re.sub(r"\\newline\s*(?=\\begin\{)", "", result)
    result = re.sub(r"(?<=\})\\newline\s*(?=\\begin\{)", "", result)
    result = re.sub(r"(\\begin\{[^}]+\})\s*\\newline\b", r"\1", result)
    result = re.sub(r"(\\end\{[^}]+\})\s*\\newline\b", r"\1 ", result)
    result = re.sub(r"\\newline\s*(?=\\item\b)", "", result)
    result = re.sub(r"\\newline\s*(?=\\end\{)", "", result)
    return result


def _format_criteria_cell(raw: str) -> str:
    """Format a marking_criteria string for the Expected column.

    Single-token criteria (one word or one number, no spaces) are grouped
    on one line joined with ' / '. Multi-word criteria each get their own line.
    """
    lines = []
    for line in raw.split("\n"):
        line = re.sub(r"^\s*\[[^\]]*\]\s*", "", line).strip()
        if line:
            lines.append(line)

    if not lines:
        return "---"

    segments: list[str] = []
    short_group: list[str] = []
    for criterion in lines:
        if " " not in criterion and not criterion.startswith("\\"):  # single token: one word or one number (not a LaTeX command)
            short_group.append(criterion)
        else:
            if short_group:
                segments.append(" / ".join(short_group))
                short_group = []
            segments.append(criterion)
    if short_group:
        segments.append(" / ".join(short_group))

    result = "\n".join(segments)
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


def _student_report_to_tex(report: dict, exam_name: str = "") -> str:
    import datetime
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
    # Column widths fill landscape A4 text width (25.7 cm - ~2.5 cm separator overhead = 22.7 cm):
    # p{0.6cm} + p{0.6cm} + p{0.7cm} + p{5.7cm} + p{7.0cm} + p{8.1cm} = 22.7 cm
    return (
        "\\documentclass{article}\n"
        "\\usepackage{fontspec}\n"
        "\\usepackage{amsmath}\n"
        "\\usepackage{amssymb}\n"
        "\\usepackage{booktabs}\n"
        "\\usepackage{longtable}\n"
        "\\usepackage{geometry}\n"
        "\\usepackage{xcolor}\n"
        "\\usepackage{array}\n"
        "\\newcolumntype{L}[1]{>{\\raggedright\\arraybackslash}p{#1}}\n"
        "\\geometry{a4paper,landscape,margin=2cm}\n"
        "\\begin{document}\n"
        f"\\section*{{Student Report: {name}{header_extra}}}\n"
        f"\\textbf{{Total: {total}/{max_m} ({pct_display} raw, {curved_display} curved)}} \\quad "
        f"\\textcolor{{gray}}{{\\small {date_str}}}\n"
        "\\vspace{1em}\n\n"
        "{\\small\n"
        "\\renewcommand{\\arraystretch}{1.6}\n"
        "\\begin{longtable}{L{0.6cm}L{0.6cm}L{0.7cm}L{5.7cm}L{7.0cm}L{8.1cm}}\n"
        "\\toprule\n"
        "\\textbf{Q} & \\textbf{Max} & \\textbf{Got} & "
        "\\textbf{Student Answer} & \\textbf{Expected} & \\textbf{Reasoning} \\\\\n"
        "\\midrule\n"
        "\\endfirsthead\n"
        "\\midrule\n"
        "\\textbf{Q} & \\textbf{Max} & \\textbf{Got} & "
        "\\textbf{Student Answer} & \\textbf{Expected} & \\textbf{Reasoning} \\\\\n"
        "\\midrule\n"
        "\\endhead\n"
        f"{rows_str}\n"
        "\\bottomrule\n"
        "\\end{longtable}\n"
        "}\n"
        "\\end{document}\n"
    )


def _class_report_to_tex(report: dict, exam_name: str = "") -> str:
    import datetime
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

    return (
        "\\documentclass{article}\n"
        "\\usepackage{fontspec}\n"
        "\\usepackage{amsmath}\n"
        "\\usepackage{amssymb}\n"
        "\\usepackage{booktabs}\n"
        "\\usepackage{longtable}\n"
        "\\usepackage{geometry}\n"
        "\\usepackage{xcolor}\n"
        "\\geometry{a4paper,margin=2cm}\n"
        "\\begin{document}\n"
        f"\\section*{{Class Report{header_extra}}}\n"
        f"\\textbf{{Class average: {'N/A' if report['class_average_pct'] is None else str(report['class_average_pct']) + '\\%'}}} \\quad\n"
        f"\\textbf{{Max marks: {report['total_max_marks']}}} \\quad\n"
        f"\\textcolor{{gray}}{{\\small {date_str}}}\n"
        "\\vspace{1em}\n\n"
        "\\subsection*{Student Rankings}\n"
        "\\begin{longtable}{rlrrr}\n"
        "\\toprule\n"
        "\\textbf{Rank} & \\textbf{Student} & \\textbf{Marks} & \\textbf{Percentage} & \\textbf{Curved} \\\\\n"
        "\\midrule\n"
        "\\endfirsthead\n"
        "\\midrule\n"
        "\\textbf{Rank} & \\textbf{Student} & \\textbf{Marks} & \\textbf{Percentage} & \\textbf{Curved} \\\\\n"
        "\\midrule\n"
        "\\endhead\n"
        f"{student_rows_str}\n"
        "\\bottomrule\n"
        "\\end{longtable}\n\n"
        "\\subsection*{Exercise Rankings (hardest first)}\n"
        "\\begin{longtable}{lrrr}\n"
        "\\toprule\n"
        "\\textbf{Question} & \\textbf{Max} & \\textbf{Class Avg} & \\textbf{Class Avg \\%} \\\\\n"
        "\\midrule\n"
        "\\endfirsthead\n"
        "\\midrule\n"
        "\\textbf{Question} & \\textbf{Max} & \\textbf{Class Avg} & \\textbf{Class Avg \\%} \\\\\n"
        "\\midrule\n"
        "\\endhead\n"
        f"{q_rows_str}\n"
        "\\bottomrule\n"
        "\\end{longtable}\n"
        "\\end{document}\n"
    )
