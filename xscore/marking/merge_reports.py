"""Step 14 — Merge per-page marking results into student and class reports.

Produces JSON + Markdown + LaTeX/PDF for each student and the class overall.
xelatex is used for compilation; a warning is printed if it is not installed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from xscore.shared.exam_paths import (
    artifact_marking_students_dir,
    artifact_reports_students_dir,
    safe_student_name as _safe_name,
)


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
}
_LATEX_RE = re.compile("|".join(re.escape(k) for k in _LATEX_MAP))


def _latex_escape(text: str) -> str:
    """Escape special LaTeX characters (single-pass to avoid double-escaping)."""
    return _LATEX_RE.sub(lambda m: _LATEX_MAP[m.group()], text)


_LATEX_PASSTHROUGH_RE = re.compile(r'(\$[^$]+\$|\\[a-zA-Z]+\{[^}]*\})')

def _latex_escape_smart(text: str) -> str:
    """Apply _latex_escape to plain text, leaving $...$ and \\command{...} blocks intact."""
    parts = _LATEX_PASSTHROUGH_RE.split(text)
    return "".join(
        part if _LATEX_PASSTHROUGH_RE.fullmatch(part) else _latex_escape(part)
        for part in parts
    )


def _restore_json_control_chars(text: str) -> str:
    """Undo JSON escape-sequence damage in AI-generated LaTeX text.

    JSON parsers interpret \\t, \\f, \\r, \\b as control characters.
    LaTeX commands starting with those letters (\\times, \\frac, \\rightarrow,
    \\boldsymbol) get silently corrupted. This restores them to backslash + letter
    before the LaTeX escape pipeline runs.
    Handles both single-escaped (\\t → tab) and double-escaped (\\\\t → backslash+tab)
    variants so AI over-escaping produces a single backslash, not two.
    \\n is left intact — _latex_newlines() needs it for table-cell line breaks.
    """
    for ctrl, letter in [("\t", "t"), ("\f", "f"), ("\r", "r"), ("\b", "b")]:
        text = text.replace("\\" + ctrl, "\\" + letter)  # backslash+ctrl → backslash+letter first
        text = text.replace(ctrl, "\\" + letter)          # bare ctrl → backslash+letter
    return text


def _latex_newlines(text: str) -> str:
    """Replace Python newlines with LaTeX \\newline for p{} table cells.

    Must be called AFTER _latex_escape_smart — if called before, the backslash
    in \\newline would itself be escaped to \\textbackslash{}newline.

    Skips $...$ math blocks: stray CR/LF inside math are stripped rather than
    converted to \\newline (safety net; _restore_json_control_chars should have
    already restored \\r artefacts to backslash+r before this is called).
    """
    parts = re.split(r"(\$[^$]+\$)", text)
    result = []
    for part in parts:
        if part.startswith("$") and part.endswith("$") and len(part) > 1:
            result.append(re.sub(r"[\r\n]", " ", part))
        else:
            part = part.replace("\r\n", "\n").replace("\r", "\n")
            # "\\newline " is a regular-string literal: one backslash → \newline in LaTeX.
            # (str.replace is literal, not a regex, so "\\" here means one backslash.)
            part = part.replace("\n", "\\newline ")
            result.append(part)
    return "".join(result)


def _strip_mark_outcome(text: str) -> str:
    """Remove the trailing mark tally the AI appends to explanations.

    Patterns stripped (case-insensitive, with em-dash, en-dash, or hyphen):
      "— 1 mark."  "— 0 marks."  "— 1 of 2 marks."  "– 2/3 marks."
    The Got column already shows the awarded marks, so this is redundant in the
    Reasoning column.
    """
    return re.sub(
        r'\s*[—–\-]\s*\d+(?:\s+of\s+\d+|\s*/\s*\d+)?\s+marks?\.?\s*$',
        '',
        text,
        flags=re.IGNORECASE,
    ).rstrip()


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
        if " " not in criterion:   # single token: one word or one number
            short_group.append(criterion)
        else:
            if short_group:
                segments.append(" / ".join(short_group))
                short_group = []
            segments.append(criterion)
    if short_group:
        segments.append(" / ".join(short_group))

    result = "\n".join(segments)
    if len(result) > 300:
        result = result[:299] + "…"
    return _latex_newlines(_latex_escape_smart(result))


# ---------------------------------------------------------------------------
# Per-student merge
# ---------------------------------------------------------------------------

def _merge_student_pages(
    artifact_dir: Path,
    student_name: str,
    pages_per_student: int,
    total_max_marks: int,
) -> dict:
    """Load all 14_marked_{student}_{p}.json and merge into one student report.

    Cross-page question strategy:
    - If only one page has assigned_marks, use that entry.
    - If both pages have assigned_marks, take the higher value.

    Duplicate question numbers on the same page (e.g. two MCQ variants both
    numbered "38") are kept as separate entries: first occurrence → "38",
    second → "38_2", etc.  Across pages, entries at the same (number, occurrence)
    slot are merged with the higher-marks strategy.
    """
    import logging
    from xscore.shared.exam_paths import artifact_marked_json_path

    merged_questions: dict[tuple[str, int], dict] = {}

    for p in range(1, pages_per_student + 1):
        path = artifact_marked_json_path(artifact_dir, student_name, p)
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        file_occ: dict[str, int] = {}
        for q in data.get("questions", []):
            qnum = q.get("number", "?")
            file_occ[qnum] = file_occ.get(qnum, 0) + 1
            key = (qnum, file_occ[qnum])
            if key not in merged_questions:
                merged_questions[key] = q.copy()
            else:
                existing_marks = merged_questions[key].get("assigned_marks")
                new_marks = q.get("assigned_marks")
                if existing_marks is None and new_marks is None:
                    logging.warning(
                        "Q%s for %s: both pages have assigned_marks=None", qnum, student_name
                    )
                elif existing_marks is None and new_marks is not None:
                    merged_questions[key] = q.copy()
                elif (existing_marks is not None and new_marks is not None
                      and new_marks > existing_marks):
                    merged_questions[key] = q.copy()

    questions_list = []
    for (qnum, occ), q_data in merged_questions.items():
        entry = q_data.copy()
        if occ > 1:
            entry["number"] = f"{qnum}_{occ}"
        questions_list.append(entry)
    total_marks = sum(q.get("assigned_marks") or 0 for q in questions_list)
    percentage = int(round(total_marks / total_max_marks * 100)) if total_max_marks > 0 else None

    return {
        "student_name": student_name,
        "total_marks": total_marks,
        "max_marks": total_max_marks,
        "percentage": percentage,
        "questions": questions_list,
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

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
        "| Question | Type | Max | Awarded | Student Answer | Correct Answer | Reasoning |",
        "|----------|------|-----|---------|----------------|----------------|-----------|",
    ]
    for q in report["questions"]:
        answer_raw = str(q.get("student_answer") or "").strip()
        answer = "*(blank)*" if not answer_raw else answer_raw.replace("|", "/")
        awarded = q.get("assigned_marks")
        awarded_str = "*?*" if awarded is None else str(awarded)
        correct = str(q.get("correct_answer") or "—").replace("|", "/")
        reasoning = str(q.get("explanation") or "").replace("|", "/")
        qtype_md = str(q.get("question_type", "")).replace("_", " ").title()
        lines.append(
            f"| {q.get('number', '')} | {qtype_md} | "
            f"{q.get('max_marks', '')} | {awarded_str} | {answer} | {correct} | {reasoning} |"
        )
    return "\n".join(lines) + "\n"


def _rank_students(students: list[dict]) -> list[dict]:
    """Return students sorted by percentage desc, each dict annotated with 'rank'.

    Ties share the same rank; the next rank skips (1, 2, 2, 4).
    Students with percentage=None sort last and receive rank=None.
    """
    sorted_s = sorted(
        students,
        key=lambda s: s["percentage"] if s["percentage"] is not None else -1,
        reverse=True,
    )
    rank = 1
    for i, s in enumerate(sorted_s):
        if i == 0:
            s["rank"] = rank if s["percentage"] is not None else None
        elif s["percentage"] is None:
            s["rank"] = None
        else:
            if s["percentage"] != sorted_s[i - 1]["percentage"]:
                rank = i + 1
            s["rank"] = rank
    return sorted_s


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


# ---------------------------------------------------------------------------
# LaTeX
# ---------------------------------------------------------------------------

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
        qtype = str(q.get("question_type", "")).replace("_", " ").title()
        max_q = q.get("max_marks", "")
        awarded = q.get("assigned_marks")
        answer_raw = str(q.get("student_answer") or "").strip()
        answer = (
            "\\textit{(blank)}" if not answer_raw
            else _latex_newlines(_latex_escape_smart(_restore_json_control_chars(answer_raw)))
        )
        correct_raw = str(q.get("correct_answer") or "").strip()
        criteria_raw = str(q.get("marking_criteria") or "").strip()
        question_type = str(q.get("question_type", "")).strip()
        if question_type == "multiple_choice" or not criteria_raw:
            # MCQ: always show the answer letter.
            # Non-MCQ without criteria: fall back to correct_answer.
            correct_ans = (
                _latex_newlines(_latex_escape_smart(_restore_json_control_chars(correct_raw)))
                if correct_raw else "---"
            )
        else:
            # Non-MCQ with criteria: show the full breakdown regardless of correct_answer.
            correct_ans = _format_criteria_cell(_restore_json_control_chars(criteria_raw))
        reasoning = _latex_newlines(_latex_escape_smart(_restore_json_control_chars(_strip_mark_outcome(str(q.get("explanation") or "")))))
        awarded_cell = _awarded_tex(awarded, max_q)
        rows.append(
            f"    {qnum} & {qtype} & {max_q} & {awarded_cell} & {answer} & {correct_ans} & {reasoning} \\\\ \\hline"
        )
    rows_str = "\n".join(rows)
    pct_display = "N/A" if pct is None else f"{pct}\\%"
    # Column widths fill landscape A4 text width (25.7 cm - ~3 cm separator overhead = 22.7 cm):
    # p{0.6cm} + p{1.5cm} + p{0.6cm} + p{0.7cm} + p{5.2cm} + p{6.5cm} + p{7.6cm} = 22.7 cm
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
        f"\\textbf{{Total: {total}/{max_m} ({pct_display})}} \\quad "
        f"\\textcolor{{gray}}{{\\small {date_str}}}\n"
        "\\vspace{1em}\n\n"
        "{\\small\n"
        "\\renewcommand{\\arraystretch}{1.6}\n"
        "\\begin{longtable}{L{0.6cm}L{1.5cm}L{0.6cm}L{0.7cm}L{5.2cm}L{6.5cm}L{7.6cm}}\n"
        "\\toprule\n"
        "\\textbf{Q} & \\textbf{Type} & \\textbf{Max} & \\textbf{Got} & "
        "\\textbf{Student Answer} & \\textbf{Expected} & \\textbf{Reasoning} \\\\\n"
        "\\midrule\n"
        "\\endfirsthead\n"
        "\\midrule\n"
        "\\textbf{Q} & \\textbf{Type} & \\textbf{Max} & \\textbf{Got} & "
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
        rank_cell = str(s["rank"]) if s.get("rank") is not None else "---"
        student_rows.append(f"    {rank_cell} & {name} & {s['total_marks']} & {pct_display} \\\\")
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
        "\\begin{longtable}{rlrl}\n"
        "\\toprule\n"
        "\\textbf{Rank} & \\textbf{Student} & \\textbf{Marks} & \\textbf{Percentage} \\\\\n"
        "\\midrule\n"
        "\\endfirsthead\n"
        "\\midrule\n"
        "\\textbf{Rank} & \\textbf{Student} & \\textbf{Marks} & \\textbf{Percentage} \\\\\n"
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


def _merge_pdfs(class_pdf: Path, students_dir: Path, output_pdf: Path) -> None:
    """Concatenate the class overview PDF with all student PDFs (alphabetical by name)."""
    from xscore.shared.terminal_ui import warn_line

    def _student_name(p: Path) -> str:
        stem = p.stem  # e.g. "15_student_report_Ashley"
        return stem.split("_student_report_", 1)[-1] if "_student_report_" in stem else stem

    student_pdfs = sorted(students_dir.glob("15_student_report_*.pdf"), key=_student_name)

    try:
        from pikepdf import Pdf

        combined = Pdf.new()
        for pdf_path in [class_pdf, *student_pdfs]:
            if not pdf_path.exists():
                warn_line(f"PDF missing, skipping from combined report: {pdf_path.name}")
                continue
            with Pdf.open(pdf_path) as src:
                combined.pages.extend(src.pages)
        combined.save(output_pdf)
    except Exception as exc:  # noqa: BLE001
        warn_line(f"Could not create combined class report: {exc}")


def _compile_tex(tex_path: Path, output_dir: Path) -> None:
    """Compile .tex with xelatex. Warns on failure but does not raise."""
    from xscore.shared.terminal_ui import warn_line

    try:
        result = subprocess.run(
            [
                "xelatex",
                "-interaction=nonstopmode",
                f"-output-directory={output_dir}",
                str(tex_path),
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode != 0:
            warn_line(
                f"xelatex returned {result.returncode} for {tex_path.name} — PDF may be missing"
            )
    except FileNotFoundError:
        warn_line("xelatex not found — PDF reports skipped (install TeX Live or MacTeX)")
    except subprocess.TimeoutExpired:
        warn_line(f"xelatex timed out for {tex_path.name}")
    except Exception as exc:  # noqa: BLE001
        warn_line(f"xelatex error for {tex_path.name}: {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _derive_student_names(artifact_dir: Path) -> list[str]:
    """Collect unique student names from 14_marked_*_*.json files, in order."""
    seen: dict[str, str] = {}   # safe_name → original name
    result: list[str] = []
    for f in sorted(artifact_marking_students_dir(artifact_dir).glob("14_marked_*_*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            name = str(data.get("student_name") or "").strip()
            if not name:
                continue
            key = _safe_name(name)
            if key not in seen:
                seen[key] = name
                result.append(name)
            elif seen[key] != name:
                # Collision: two distinct names share the same sanitised key.
                # Append a numeric suffix so neither is silently dropped.
                suffix = 2
                while f"{key}_{suffix}" in seen:
                    suffix += 1
                unique_key = f"{key}_{suffix}"
                seen[unique_key] = name
                result.append(name)
        except Exception:  # noqa: BLE001
            pass
    return result


def _build_all_question_tables(
    questions: list,
    leaf_avgs: dict[str, float],
) -> tuple[dict[str, float], dict[str, int]]:
    """Return (all_avgs, all_max) for every question node including parents.

    Leaf averages come directly from leaf_avgs (keyed with _N suffixes for duplicates).
    Parent averages are the rounded sum of their direct children's averages (recursive).
    all_max is keyed with the same _N suffix convention using a seen counter.
    """
    from xscore.shared.models import flatten_questions

    def _subtree_avg(q) -> float | None:
        if not q.subquestions:
            return leaf_avgs.get(str(q.number or ""))
        parts = [_subtree_avg(c) for c in q.subquestions]
        valid = [p for p in parts if p is not None]
        return round(sum(valid), 1) if valid else None

    all_avgs: dict[str, float] = dict(leaf_avgs)
    all_max: dict[str, int] = {}
    seen: dict[str, int] = {}
    for q in flatten_questions(questions):
        num = str(q.number or "")
        if not num:
            continue
        seen[num] = seen.get(num, 0) + 1
        key = num if seen[num] == 1 else f"{num}_{seen[num]}"
        all_max[key] = int(q.marks or 0)
        if q.subquestions:
            avg = _subtree_avg(q)
            if avg is not None:
                all_avgs[key] = avg
    return all_avgs, all_max


def _compute_per_question_averages(artifact_dir: Path) -> dict[str, float]:
    """Compute mean assigned_marks per question number across all student reports."""
    q_totals: dict[str, list[float]] = {}
    for f in sorted(artifact_reports_students_dir(artifact_dir).glob("15_student_report_*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            for q in data.get("questions", []):
                qnum = q.get("number", "?")
                marks = q.get("assigned_marks")
                if marks is not None:
                    q_totals.setdefault(qnum, []).append(float(marks))
        except Exception:  # noqa: BLE001
            pass
    return {k: round(sum(v) / len(v), 1) for k, v in q_totals.items()}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def compile_reports(ctx: Any) -> list[dict]:
    """Merge all per-page results; create student and class reports; compile PDFs.

    Returns a list of per-student summary dicts
    (keys: name, total_marks, percentage) for use in step 14 timing.
    """
    from xscore.shared.exam_paths import (
        artifact_class_report_json_path,
        artifact_class_report_md_path,
        artifact_class_report_tex_path,
        artifact_marked_json_path,
        artifact_student_report_json_path,
        artifact_student_report_md_path,
        artifact_student_report_tex_path,
    )
    from xscore.shared.terminal_ui import info_line

    total_max_marks = ctx.scaffold.total_marks
    student_summaries: list[dict] = []
    tex_paths: list[Path] = []

    # Build correct_answer lookup keyed by (possibly _2-suffixed) question number so it
    # matches the renamed numbers produced by _merge_student_pages.
    correct_answers: dict[str, str] = {}
    marking_criteria_by_num: dict[str, str] = {}
    seen_ca: dict[str, int] = {}
    for _q in ctx.scaffold.gradable_questions:
        seen_ca[_q.number] = seen_ca.get(_q.number, 0) + 1
        _occ = seen_ca[_q.number]
        _key = _q.number if _occ == 1 else f"{_q.number}_{_occ}"
        correct_answers[_key] = _q.correct_answer or ""
        marking_criteria_by_num[_key] = _q.marking_criteria or ""

    # Pass 1 — sequential: merge marks and write all data files (fast I/O, order-sensitive)
    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_marking_students_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    for name in _derive_student_names(ctx.artifact_dir):
        report = _merge_student_pages(
            ctx.artifact_dir, name, ctx.pages_per_student, total_max_marks
        )
        # Annotate each question with the correct answer and marking criteria from the scaffold.
        for _q in report["questions"]:
            _q["correct_answer"] = correct_answers.get(str(_q.get("number", "")), "")
            _q["marking_criteria"] = marking_criteria_by_num.get(str(_q.get("number", "")), "")

        artifact_student_report_json_path(ctx.artifact_dir, name).write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        artifact_student_report_md_path(ctx.artifact_dir, name).write_text(
            _student_report_to_md(report), encoding="utf-8"
        )
        tex_path = artifact_student_report_tex_path(ctx.artifact_dir, name)
        exam_name = ctx.artifact_dir.parent.name
        tex_path.write_text(_student_report_to_tex(report, exam_name=exam_name), encoding="utf-8")
        tex_paths.append(tex_path)

        student_summaries.append({
            "name": name,
            "total_marks": report["total_marks"],
            "percentage": report["percentage"],
        })
        info_line(
            f"{name}: {report['total_marks']}/{total_max_marks} ({_fmt_pct(report['percentage'])})"
        )

    # Pass 2 — parallel: compile all student .tex files concurrently (each is an independent process)
    workers = int(os.environ.get("REPORT_COMPILE_WORKERS", os.environ.get("MARKING_WORKERS", "4")))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda p: _compile_tex(p, p.parent), tex_paths))

    if student_summaries:
        leaf_avgs = _compute_per_question_averages(ctx.artifact_dir)
        all_avgs, all_max = _build_all_question_tables(
            getattr(ctx.scaffold, "questions", []), leaf_avgs
        )
        per_question_pct: dict[str, int] = {
            qnum: int(round(avg / all_max[qnum] * 100))
            for qnum, avg in all_avgs.items()
            if all_max.get(qnum, 0) > 0
        }
        known_pcts = [s["percentage"] for s in student_summaries if s["percentage"] is not None]
        class_avg = int(round(sum(known_pcts) / len(known_pcts))) if known_pcts else None
        ranked_students = _rank_students(student_summaries)
        class_report = {
            "students": ranked_students,
            "per_question_averages": all_avgs,
            "per_question_max_marks": all_max,
            "per_question_pct_averages": per_question_pct,
            "class_average_pct": class_avg,
            "total_max_marks": total_max_marks,
        }

        artifact_class_report_json_path(ctx.artifact_dir).write_text(
            json.dumps(class_report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        artifact_class_report_md_path(ctx.artifact_dir).write_text(
            _class_report_to_md(class_report), encoding="utf-8"
        )
        exam_name = ctx.artifact_dir.parent.name
        tex_path = artifact_class_report_tex_path(ctx.artifact_dir)
        tex_path.write_text(_class_report_to_tex(class_report, exam_name=exam_name), encoding="utf-8")
        _compile_tex(tex_path, tex_path.parent)
        _merge_pdfs(
            tex_path.with_suffix(".pdf"),
            artifact_reports_students_dir(ctx.artifact_dir),
            tex_path.parent / "15_class_report_combined.pdf",
        )
        info_line(f"Class average: {_fmt_pct(class_avg)}")

    return student_summaries


def load_student_results_from_reports(artifact_dir: Path) -> list:
    """Read all 15_student_report_*.json and reconstruct StudentResult objects.

    Used by step 15 to compare AI-extracted answers against ground truth.
    """
    from xscore.shared.models import StudentResult

    results = []
    for f in sorted(artifact_reports_students_dir(artifact_dir).glob("15_student_report_*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        name = data.get("student_name", "")
        if not name:
            continue
        answers: dict[str, str] = {}
        marks_per_q: dict[str, float] = {}
        for q in data.get("questions", []):
            qnum = str(q.get("number", ""))
            if not qnum:
                continue
            ans = q.get("student_answer")
            answers[qnum] = str(ans).strip() if ans is not None else "?"
            m = q.get("assigned_marks")
            marks_per_q[qnum] = float(m) if m is not None else 0.0
        results.append(StudentResult(
            student_name=name,
            page_numbers=[],
            answers=answers,
            marks_per_question=marks_per_q,
            total_marks=float(data.get("total_marks", 0)),
            max_marks=float(data.get("max_marks", 0)),
        ))
    return results
