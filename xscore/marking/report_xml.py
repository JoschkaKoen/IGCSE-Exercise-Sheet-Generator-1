"""Report serialisers for the marking phase.

- :func:`student_report_to_yaml` — per-student report (per_student_reports). Uses
  ``_MarkingDumper`` so LaTeX in ``mark_scheme_answer`` / ``student_answer`` /
  ``explanation`` is preserved as ``|`` block scalars (matches steps 19/20,
  23/24, 27/28, 29).
- :func:`class_report_to_xml` — class-level summary (class_report). Still XML for
  now; migrating it is a separate cleanup.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import yaml

from xscore.marking.formats.base import _MarkingDumper


def student_report_to_yaml(report: dict) -> str:
    """Serialise a merged student report dict to YAML.

    Strips the internal ``_unanswered`` marker from each question (added by
    ``_augment_with_unanswered`` for downstream rendering — has no place in
    the on-disk artefact).
    """
    out = {k: v for k, v in report.items() if k != "questions"}
    out["questions"] = [
        {k: v for k, v in q.items() if k != "_unanswered"}
        for q in (report.get("questions") or [])
    ]
    return yaml.dump(
        out, Dumper=_MarkingDumper,
        allow_unicode=True, default_flow_style=False, sort_keys=False,
    )


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

    top_avgs = report.get("per_top_question_averages") or {}
    if top_avgs:
        top_max = report.get("per_top_question_max_marks") or {}
        top_pct = report.get("per_top_question_pct_averages") or {}
        top_el = ET.SubElement(root, "per_top_question_averages")
        for qnum, avg in top_avgs.items():
            qel = ET.SubElement(top_el, "question")
            qel.set("number", str(qnum))
            qel.set("avg", str(avg))
            qel.set("max_marks", str(top_max.get(qnum, "")))
            qel.set("avg_pct", str(top_pct.get(qnum, "")))

    ET.indent(root)
    return ET.tostring(root, encoding="unicode")
