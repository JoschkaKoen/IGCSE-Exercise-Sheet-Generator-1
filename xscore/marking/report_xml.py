"""XML serialisation for student / class reports + post-hoc XML loader.

All round-trips for ``student_report.xml`` and ``class_report.xml`` live here
so schema changes only touch one file.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from xscore.shared.exam_paths import artifact_reports_students_dir
from xscore.shared.terminal_ui import warn_line


def student_report_to_xml(report: dict) -> str:
    """Serialise a merged student report dict to XML.

    Scalar values (marks, question metadata) are stored as attributes.
    LaTeX content (marking_criteria, student_answer, explanation) is stored
    as child elements so no JSON escaping conflicts arise.
    """
    root = ET.Element("student_report")
    root.set("student_name", str(report.get("student_name") or ""))
    root.set("total_marks", str(report.get("total_marks", 0)))
    root.set("max_marks", str(report.get("max_marks", 0)))
    root.set("percentage", str(report.get("percentage", "")))

    for q in report.get("questions") or []:
        qel = ET.SubElement(root, "question")
        qel.set("number", str(q.get("number", "")))
        qel.set("question_type", str(q.get("question_type", "")))
        qel.set("max_marks", str(q.get("max_marks", 0)))
        assigned = q.get("assigned_marks")
        qel.set("assigned_marks", str(assigned) if assigned is not None else "")
        qel.set("correct_answer", str(q.get("correct_answer") or ""))

        mc_el = ET.SubElement(qel, "marking_criteria")
        mc_el.text = str(q.get("marking_criteria") or "")

        sa_el = ET.SubElement(qel, "student_answer")
        sa_el.text = str(q.get("student_answer") or "")

        exp_el = ET.SubElement(qel, "explanation")
        exp_el.text = str(q.get("explanation") or "")

    ET.indent(root)
    return ET.tostring(root, encoding="unicode")


def class_report_to_xml(report: dict) -> str:
    """Serialise a class report dict to XML."""
    root = ET.Element("class_report")
    root.set("class_average_pct", str(report.get("class_average_pct", "")))
    root.set("total_max_marks", str(report.get("total_max_marks", 0)))

    students_el = ET.SubElement(root, "students")
    for s in report.get("students") or []:
        sel = ET.SubElement(students_el, "student")
        sel.set("name", str(s.get("name", "")))
        sel.set("total_marks", str(s.get("total_marks", 0)))
        sel.set("percentage", str(s.get("percentage", "")))
        sel.set("rank", str(s.get("rank", "")))

    avgs_el = ET.SubElement(root, "per_question_averages")
    all_avgs = report.get("per_question_averages") or {}
    all_max = report.get("per_question_max_marks") or {}
    pct_avgs = report.get("per_question_pct_averages") or {}
    for qnum, avg in all_avgs.items():
        qel = ET.SubElement(avgs_el, "question")
        qel.set("number", str(qnum))
        qel.set("avg", str(avg))
        qel.set("max_marks", str(all_max.get(qnum, "")))
        qel.set("avg_pct", str(pct_avgs.get(qnum, "")))

    ET.indent(root)
    return ET.tostring(root, encoding="unicode")


def _compute_per_question_averages(artifact_dir: Path) -> dict[str, float]:
    """Compute mean assigned_marks per question number across all student reports."""
    q_totals: dict[str, list[float]] = {}
    failed: list[str] = []
    for f in sorted(artifact_reports_students_dir(artifact_dir).glob("*/*.xml")):
        try:
            root = ET.parse(str(f)).getroot()
            for qel in root.findall("question"):
                qnum = qel.get("number", "?")
                marks_str = qel.get("assigned_marks", "")
                try:
                    marks: float | None = float(marks_str)
                except (ValueError, TypeError):
                    marks = None
                if marks is not None:
                    q_totals.setdefault(qnum, []).append(marks)
        except Exception:  # noqa: BLE001
            failed.append(f.name)
    if failed:
        warn_line(
            f"{len(failed)} student report XML file(s) skipped from per-question averages: "
            + ", ".join(failed)
        )
    return {k: round(sum(v) / len(v), 1) for k, v in q_totals.items()}


def load_student_results_from_reports(artifact_dir: Path) -> list:
    """Read all student report XML files and reconstruct StudentResult objects."""
    from xscore.shared.models import StudentResult

    results = []
    failed: list[str] = []
    for f in sorted(artifact_reports_students_dir(artifact_dir).glob("*/*.xml")):
        try:
            root = ET.parse(str(f)).getroot()
        except ET.ParseError:
            failed.append(f.name)
            continue
        name = root.get("student_name", "")
        if not name:
            continue
        answers: dict[str, str] = {}
        marks_per_q: dict[str, float] = {}
        for qel in root.findall("question"):
            qnum = str(qel.get("number", ""))
            if not qnum:
                continue
            sa_el = qel.find("student_answer")
            ans = (sa_el.text or "") if sa_el is not None else ""
            answers[qnum] = ans.strip() if ans else "?"
            marks_str = qel.get("assigned_marks", "")
            try:
                marks_per_q[qnum] = float(marks_str)
            except (ValueError, TypeError):
                marks_per_q[qnum] = 0.0
        try:
            total_marks = float(root.get("total_marks", 0))
            max_marks = float(root.get("max_marks", 0))
        except (ValueError, TypeError):
            total_marks = 0.0
            max_marks = 0.0
        results.append(StudentResult(
            student_name=name,
            page_numbers=[],
            answers=answers,
            marks_per_question=marks_per_q,
            total_marks=total_marks,
            max_marks=max_marks,
        ))
    if failed:
        warn_line(
            f"{len(failed)} student report XML file(s) could not be parsed for accuracy comparison: "
            + ", ".join(failed)
        )
    return results
