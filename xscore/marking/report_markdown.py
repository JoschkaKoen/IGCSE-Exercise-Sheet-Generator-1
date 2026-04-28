"""Markdown rendering for student and class reports.

Mirrors :mod:`xscore.marking.report_latex` but emits Markdown tables instead
of LaTeX. Pure functions: dict in, str out.
"""

from __future__ import annotations


def _fmt_pct(pct: float | None) -> str:
    return "N/A" if pct is None else f"{pct}%"


def _student_report_to_md(report: dict) -> str:
    name = report["student_name"]
    total = report["total_marks"]
    max_m = report["max_marks"]
    pct = report["percentage"]
    lines = [
        f"# Student Report: {name}\n",
        f"**Total: {total}/{max_m} ({_fmt_pct(pct)})**\n",
        "| Question | Max | Awarded | Student Answer | Correct Answer | Reasoning |",
        "|----------|-----|---------|----------------|----------------|-----------|",
    ]
    for q in report["questions"]:
        answer_raw = str(q.get("student_answer") or "").strip()
        if q.get("_unanswered"):
            answer = "*(not answered)*"
        elif not answer_raw:
            answer = "*(blank)*"
        else:
            answer = answer_raw.replace("|", "/")
        awarded = q.get("assigned_marks")
        awarded_str = "*?*" if awarded is None else str(awarded)
        correct = str(q.get("correct_answer") or "—").replace("|", "/")
        reasoning = str(q.get("explanation") or "").replace("|", "/")
        lines.append(
            f"| {q.get('number', '')} | "
            f"{q.get('max_marks', '')} | {awarded_str} | {answer} | {correct} | {reasoning} |"
        )
    return "\n".join(lines) + "\n"


def _class_report_to_md(report: dict) -> str:
    lines = [
        "# Class Report\n",
        f"**Class average: {_fmt_pct(report['class_average_pct'])}  |  Max marks: {report['total_max_marks']}**\n",
        "## Student Rankings\n",
        "| Rank | Student | Marks | Percentage |",
        "|------|---------|-------|------------|",
    ]
    for s in report["students"]:
        rank_cell = str(s["rank"]) if s.get("rank") is not None else "—"
        lines.append(f"| {rank_cell} | {s['name']} | {s['total_marks']} | {_fmt_pct(s['percentage'])} |")
    if report.get("per_question_averages"):
        lines.append("\n## Exercise Rankings (hardest first)\n")
        lines.append("| Question | Max | Class Avg | Class Avg % |")
        lines.append("|----------|-----|-----------|-------------|")
        q_max = report.get("per_question_max_marks", {})
        q_pct = report.get("per_question_pct_averages", {})
        for qnum, avg in sorted(
            report["per_question_averages"].items(),
            key=lambda x: (q_pct.get(x[0], float("inf")), x[0]),
        ):
            max_cell = q_max.get(qnum, "")
            pct_cell = f"{q_pct[qnum]}%" if qnum in q_pct else "N/A"
            lines.append(f"| {qnum} | {max_cell} | {avg} | {pct_cell} |")
    return "\n".join(lines) + "\n"
