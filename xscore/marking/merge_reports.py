"""Step 14 — Merge per-page marking results into student and class reports.

Produces XML + Markdown + LaTeX/PDF for each student and the class overall.
xelatex is used for compilation; a warning is printed if it is not installed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from xscore.marking.formats import get_marking_format
from xscore.marking.report_latex import (
    _latex_escape, _ai_cell, _format_criteria_cell,
    _awarded_tex, _student_report_to_tex, _class_report_to_tex,
)
from xscore.shared.exam_paths import (
    artifact_class_grade_histogram_path,
    artifact_class_question_difficulty_path,
    artifact_class_report_combined_landscape_pdf_path,
    artifact_class_report_combined_portrait_2up_pdf_path,
    artifact_class_report_combined_portrait_pdf_path,
    artifact_class_report_dir,
    artifact_class_report_md_path,
    artifact_class_report_pdf_2up_path,
    artifact_class_report_tex_path,
    artifact_class_report_xml_path,
    artifact_class_stats_json_path,
    artifact_marked_path,
    artifact_marking_students_dir,
    artifact_reports_students_dir,
    artifact_review_queue_json_path,
    artifact_review_queue_md_path,
    artifact_student_pdfs_dir,
    artifact_student_report_md_path,
    artifact_student_report_pdf_portrait_2up_path,
    artifact_student_report_pdf_portrait_large_path,
    artifact_student_report_pdf_portrait_path,
    artifact_student_report_tex_landscape_path,
    artifact_student_report_tex_portrait_large_path,
    artifact_student_report_tex_portrait_path,
    artifact_student_report_xml_path,
    artifact_student_reports_dir,
    safe_student_name as _safe_name,
)
from eXercise.pdfjam_post import make_2up_landscape_pdf
from xscore.shared.terminal_ui import ok_line, warn_line


# ---------------------------------------------------------------------------
# Env-var knobs (see default.env "Phase 8 — Reports" section)
# ---------------------------------------------------------------------------

def _grade_curve_target() -> int:
    """Read GRADE_CURVE_TARGET (default 80). Used as the env-var fallback
    when the natural-language prompt doesn't override the target."""
    raw = os.environ.get("GRADE_CURVE_TARGET", "80")
    try:
        return int(raw)
    except ValueError:
        warn_line(f"Invalid GRADE_CURVE_TARGET={raw!r} — using default 80")
        return 80


def _effective_curve_target(ctx: Any) -> int:
    """Resolve the curve target for *ctx*.

    Priority: ``ctx.instruction.curved_grade_override`` (if int) → env var
    ``GRADE_CURVE_TARGET`` (default 80).
    """
    instr = getattr(ctx, "instruction", None)
    if instr is not None:
        override = getattr(instr, "curved_grade_override", None)
        if override is not None:
            return int(override)
    return _grade_curve_target()


_TRUE_STRS  = {"true",  "1", "yes", "on"}
_FALSE_STRS = {"false", "0", "no",  "off"}


def _curved_grade_visible(ctx: Any) -> bool:
    """Resolve whether per-student PDFs include the curved % in their header.

    Priority: ``ctx.instruction.curved_grade_visible`` (if bool) → env var
    ``CURVED_GRADE_VISIBLE`` (default true). Unrecognised env values warn
    and fall back to True.
    """
    instr = getattr(ctx, "instruction", None)
    if instr is not None:
        override = getattr(instr, "curved_grade_visible", None)
        if override is not None:
            return bool(override)
    raw = os.environ.get("CURVED_GRADE_VISIBLE", "true").strip().lower()
    if raw in _TRUE_STRS:
        return True
    if raw in _FALSE_STRS:
        return False
    warn_line(f"Invalid CURVED_GRADE_VISIBLE={raw!r} — using default true")
    return True


def _xelatex_timeout() -> int:
    """Read XELATEX_TIMEOUT in seconds (default 60). Used by _compile_tex."""
    raw = os.environ.get("XELATEX_TIMEOUT", "60")
    try:
        return max(1, int(raw))
    except ValueError:
        warn_line(f"Invalid XELATEX_TIMEOUT={raw!r} — using default 60s")
        return 60


# ---------------------------------------------------------------------------
# Per-student merge
# ---------------------------------------------------------------------------

def _resolve_mark_collision(
    existing: dict, new_q: dict, qnum: str, student: str, page: int,
    collisions: list[dict] | None = None,
    collisions_lock: "threading.Lock | None" = None,
) -> dict:
    """Return the winning question dict when the same question appears on multiple pages.

    Always warns; takes the higher mark when both are set. If a ``collisions``
    accumulator and lock are provided, also records the collision for the
    review queue (step 29).
    """
    em = existing.get("assigned_marks")
    nm = new_q.get("assigned_marks")

    def _record(winner: str) -> None:
        if collisions is None or collisions_lock is None:
            return
        with collisions_lock:
            collisions.append({
                "student":       student,
                "question":      qnum,
                "page":          page,
                "earlier_marks": em,
                "page_marks":    nm,
                "winner":        winner,
            })

    if em is None and nm is None:
        warn_line(f"Merged Q{qnum} for {student}: both pages have assigned_marks=None")
        _record("both_none")
        return existing
    if em is None:
        warn_line(f"Merged Q{qnum} for {student}: page {page} = {nm}, earlier = None → keeping {nm}")
        _record("page_only")
        return new_q.copy()
    if nm is None:
        warn_line(f"Merged Q{qnum} for {student}: page {page} = None, earlier = {em} → keeping {em}")
        _record("earlier_only")
        return existing
    if nm > em:
        warn_line(f"Merged Q{qnum} for {student}: page {page} = {nm}, earlier = {em} → keeping page {page} ({nm})")
        _record("page")
        return new_q.copy()
    if nm < em:
        warn_line(f"Merged Q{qnum} for {student}: page {page} = {nm}, earlier = {em} → keeping earlier ({em})")
        _record("earlier")
        return existing
    warn_line(f"Merged Q{qnum} for {student}: page {page} = {nm}, earlier = {em} → tie, keeping earlier page")
    _record("tie")
    return existing


def _merge_student_pages(
    artifact_dir: Path,
    student_name: str,
    pages_per_student: int,
    total_max_marks: int,
    fmt=None,
    collisions: list[dict] | None = None,
    collisions_lock: "threading.Lock | None" = None,
) -> dict:
    """Load all marked files for one student and merge into one report dict.

    Cross-page question strategy:
    - If only one page has assigned_marks, use that entry.
    - If both pages have assigned_marks, take the higher value.

    Duplicate question numbers on the same page (e.g. two MCQ variants both
    numbered "38") are kept as separate entries: first occurrence → "38",
    second → "38_2", etc.  Across pages, entries at the same (number, occurrence)
    slot are merged with the higher-marks strategy.

    If ``collisions`` and ``collisions_lock`` are provided, cross-page mark
    collisions are recorded for the review queue (step 29).
    """
    if fmt is None:
        from xscore.marking.formats.xml_format import XmlMarkingFormat
        fmt = XmlMarkingFormat()

    merged_questions: dict[tuple[str, int], dict] = {}

    for p in range(1, pages_per_student + 1):
        path = artifact_marked_path(artifact_dir, student_name, p, fmt=fmt.artifact_ext())
        if not path.is_file():
            continue
        file_occ: dict[str, int] = {}
        parsed = fmt.deserialize_blueprint(path.read_text(encoding="utf-8"))
        for q in parsed.get("questions", []):
            qnum = q.get("number", "?")
            file_occ[qnum] = file_occ.get(qnum, 0) + 1
            key = (qnum, file_occ[qnum])
            q_with_page = q.copy()
            q_with_page["page_label"] = p
            if key not in merged_questions:
                merged_questions[key] = q_with_page
            else:
                merged_questions[key] = _resolve_mark_collision(
                    merged_questions[key], q_with_page, qnum, student_name, p,
                    collisions=collisions, collisions_lock=collisions_lock,
                )

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
        "| Question | Max | Awarded | Student Answer | Correct Answer | Reasoning |",
        "|----------|-----|---------|----------------|----------------|-----------|",
    ]
    for q in report["questions"]:
        answer_raw = str(q.get("student_answer") or "").strip()
        answer = "*(blank)*" if not answer_raw else answer_raw.replace("|", "/")
        awarded = q.get("assigned_marks")
        awarded_str = "*?*" if awarded is None else str(awarded)
        correct = str(q.get("correct_answer") or "—").replace("|", "/")
        reasoning = str(q.get("explanation") or "").replace("|", "/")
        lines.append(
            f"| {q.get('number', '')} | "
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


def _merge_pdfs(class_pdf: Path, students_dir: Path, output_pdf: Path, suffix: str) -> None:
    """Concatenate the class overview PDF with student PDFs matching ``*_<suffix>.pdf``."""
    student_pdfs = sorted(students_dir.glob(f"*_{suffix}.pdf"), key=lambda p: p.stem)

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
    try:
        result = subprocess.run(
            [
                "xelatex",
                "-interaction=nonstopmode",
                f"-output-directory={output_dir}",
                str(tex_path),
            ],
            capture_output=True,
            timeout=_xelatex_timeout(),
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

def _derive_student_names(artifact_dir: Path, fmt=None) -> list[str]:
    """Collect unique student names from marked student files, in order."""
    if fmt is None:
        from xscore.marking.formats.xml_format import XmlMarkingFormat
        fmt = XmlMarkingFormat()
    _ext = fmt.artifact_ext()
    seen: dict[str, str] = {}   # safe_name → original name
    result: list[str] = []
    failed: list[str] = []
    # New layout: Alice_Smith_page_1.yaml; legacy: 14_marked_Alice_Smith_1.yaml
    _students_dir = artifact_marking_students_dir(artifact_dir)
    _files = sorted(_students_dir.glob(f"*_page_*.{_ext}"))
    if not _files:
        _files = sorted(_students_dir.glob(f"14_marked_*_*.{_ext}"))
    for f in _files:
        try:
            data = fmt.deserialize_blueprint(f.read_text(encoding="utf-8"))
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
            failed.append(f.name)
    if failed:
        warn_line(
            f"{len(failed)} marked XML file(s) could not be parsed and will be skipped: "
            + ", ".join(failed)
        )
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
    failed: list[str] = []
    for f in sorted(artifact_reports_students_dir(artifact_dir).glob("*.xml")):
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


# ---------------------------------------------------------------------------
# XML serialisers for step-15 output files
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# compile_reports() helpers
# ---------------------------------------------------------------------------

def _build_answer_lookup(ctx: Any) -> tuple[dict[str, str], dict[str, str]]:
    """Build correct_answer and marking_criteria dicts keyed by (possibly _N-suffixed) question number."""
    correct_answers: dict[str, str] = {}
    marking_criteria_by_num: dict[str, str] = {}
    seen: dict[str, int] = {}
    for q in ctx.scaffold.gradable_questions:
        seen[q.number] = seen.get(q.number, 0) + 1
        key = q.number if seen[q.number] == 1 else f"{q.number}_{seen[q.number]}"
        correct_answers[key] = q.correct_answer or ""
        marking_criteria_by_num[key] = q.marking_criteria or ""
    return correct_answers, marking_criteria_by_num


def _pass1_merge_students(
    ctx: Any,
    fmt: Any,
    names: list[str],
    total_max_marks: int,
    correct_answers: dict[str, str],
    marking_criteria_by_num: dict[str, str],
    workers: int,
) -> tuple[list[dict], dict[str, dict], dict[str, list[float]], list[dict], list[dict]]:
    """Parallel: merge per-page marks, write XML + MD per student, accumulate q_totals.

    Returns (student_summaries, full_reports, q_totals, failed, collisions).
    Per-student failures are collected into ``failed`` and the run continues —
    one bad student does not block the rest. ``collisions`` records cross-page
    mark conflicts for the review queue.
    """
    student_summaries: list[dict] = []
    full_reports: dict[str, dict] = {}
    q_totals: dict[str, list[float]] = {}
    collisions: list[dict] = []
    _summaries_lock = threading.Lock()
    _q_totals_lock = threading.Lock()
    _collisions_lock = threading.Lock()

    def _process_one(name: str) -> None:
        report = _merge_student_pages(
            ctx.artifact_dir, name, ctx.pages_per_student, total_max_marks, fmt=fmt,
            collisions=collisions, collisions_lock=_collisions_lock,
        )
        for q in report["questions"]:
            q["correct_answer"] = correct_answers.get(str(q.get("number", "")), "")
            q["marking_criteria"] = marking_criteria_by_num.get(str(q.get("number", "")), "")

        artifact_student_report_xml_path(ctx.artifact_dir, name).write_text(
            student_report_to_xml(report), encoding="utf-8"
        )
        artifact_student_report_md_path(ctx.artifact_dir, name).write_text(
            _student_report_to_md(report), encoding="utf-8"
        )

        with _q_totals_lock:
            for q in report["questions"]:
                am = q.get("assigned_marks")
                if am is not None:
                    q_totals.setdefault(str(q.get("number", "")), []).append(float(am))

        with _summaries_lock:
            student_summaries.append({
                "name": name,
                "total_marks": report["total_marks"],
                "percentage": report["percentage"],
            })
            full_reports[name] = report

        ok_line(f"{name}: {report['total_marks']}/{total_max_marks} ({_fmt_pct(report['percentage'])})")

    failed: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        submitted = [(name, ex.submit(_process_one, name)) for name in names]
        for name, fut in submitted:
            exc = fut.exception()
            if exc is not None:
                failed.append({
                    "name":          name,
                    "error_type":    type(exc).__name__,
                    "error_message": str(exc),
                })
                warn_line(f"merge failed for {name}: {type(exc).__name__}: {exc}")

    return student_summaries, full_reports, q_totals, failed, collisions


def _apply_grade_curve(student_summaries: list[dict], target: int) -> None:
    """Compute curve offset (target − class_avg); add curved_pct to each summary in place."""
    known_pcts = [s["percentage"] for s in student_summaries if s["percentage"] is not None]
    class_avg = int(round(sum(known_pcts) / len(known_pcts))) if known_pcts else None
    curve_offset = (target - class_avg) if class_avg is not None else 0
    for s in student_summaries:
        s["curved_pct"] = (
            min(100, max(0, s["percentage"] + curve_offset))
            if s["percentage"] is not None else None
        )


def _pass2_write_tex(
    student_summaries: list[dict],
    full_reports: dict[str, dict],
    artifact_dir: Path,
    exam_name: str,
    workers: int,
    show_curved_grade: bool = True,
) -> None:
    """Write per-student .tex files (landscape + portrait + portrait-large), then compile all in parallel."""
    tex_paths: list[Path] = []
    for s in student_summaries:
        report = full_reports[s["name"]]
        report["curved_pct"] = s["curved_pct"]
        for orientation, path_fn, font_size in (
            ("landscape", artifact_student_report_tex_landscape_path,      10),
            ("portrait",  artifact_student_report_tex_portrait_path,       10),
            ("portrait",  artifact_student_report_tex_portrait_large_path, 12),
        ):
            tex_path = path_fn(artifact_dir, s["name"])
            tex_path.write_text(
                _student_report_to_tex(
                    report, exam_name=exam_name, orientation=orientation,
                    font_size=font_size, show_curved_grade=show_curved_grade,
                ),
                encoding="utf-8",
            )
            tex_paths.append(tex_path)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(lambda p: _compile_tex(p, p.parent), tex_paths))

    portrait_2up_jobs: list[tuple[Path, Path]] = []
    for s in student_summaries:
        p_in = artifact_student_report_pdf_portrait_large_path(artifact_dir, s["name"])
        p_out = artifact_student_report_pdf_portrait_2up_path(artifact_dir, s["name"])
        if p_in.is_file():
            portrait_2up_jobs.append((p_in, p_out))
    if portrait_2up_jobs:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(lambda j: make_2up_landscape_pdf(j[0], j[1]), portrait_2up_jobs))


def _build_class_report(
    ctx: Any,
    student_summaries: list[dict],
    q_totals: dict[str, list[float]],
    exam_name: str,
) -> None:
    """Build and write class XML/MD/TeX/PDF. Runs after both passes."""
    total_max_marks = ctx.scaffold.total_marks
    leaf_avgs = {k: round(sum(v) / len(v), 1) for k, v in q_totals.items()}
    all_avgs, all_max = _build_all_question_tables(
        getattr(ctx.scaffold, "questions", []), leaf_avgs
    )
    known_pcts = [s["percentage"] for s in student_summaries if s["percentage"] is not None]
    class_avg = int(round(sum(known_pcts) / len(known_pcts))) if known_pcts else None
    per_question_pct: dict[str, int] = {
        qnum: int(round(avg / all_max[qnum] * 100))
        for qnum, avg in all_avgs.items()
        if all_max.get(qnum, 0) > 0
    }
    # Leaf-only percentages for the difficulty chart (parents would double-count).
    leaf_pct: dict[str, int] = {
        qnum: int(round(avg / all_max[qnum] * 100))
        for qnum, avg in leaf_avgs.items()
        if all_max.get(qnum, 0) > 0
    }
    # Charts are best-effort: if matplotlib isn't installed the LaTeX block
    # for the figure stays empty (template uses ``<% if histogram_path %>``).
    histogram_path: str | None = None
    difficulty_path: str | None = None
    try:
        from xscore.marking.class_charts import (
            render_grade_histogram, render_question_difficulty,
        )
        h = render_grade_histogram(
            student_summaries,
            artifact_class_grade_histogram_path(ctx.artifact_dir),
        )
        if h is not None:
            histogram_path = str(h)
        d = render_question_difficulty(
            leaf_pct, all_max,
            artifact_class_question_difficulty_path(ctx.artifact_dir),
        )
        if d is not None:
            difficulty_path = str(d)
    except ImportError:
        warn_line("matplotlib not installed — class report figures skipped")
    except Exception as exc:  # noqa: BLE001
        warn_line(f"class chart rendering failed: {type(exc).__name__}: {exc}")

    class_report = {
        "students": _rank_students(student_summaries),
        "per_question_averages": all_avgs,
        "per_question_max_marks": all_max,
        "per_question_pct_averages": per_question_pct,
        "class_average_pct": class_avg,
        "total_max_marks": total_max_marks,
        "histogram_path": histogram_path,
        "difficulty_path": difficulty_path,
    }
    artifact_class_report_xml_path(ctx.artifact_dir).write_text(
        class_report_to_xml(class_report), encoding="utf-8"
    )
    artifact_class_report_md_path(ctx.artifact_dir).write_text(
        _class_report_to_md(class_report), encoding="utf-8"
    )
    tex_path = artifact_class_report_tex_path(ctx.artifact_dir)
    tex_path.write_text(_class_report_to_tex(class_report, exam_name=exam_name), encoding="utf-8")
    _compile_tex(tex_path, tex_path.parent)
    _merge_pdfs(
        tex_path.with_suffix(".pdf"),
        artifact_student_pdfs_dir(ctx.artifact_dir),
        artifact_class_report_combined_landscape_pdf_path(ctx.artifact_dir),
        suffix="landscape",
    )
    _merge_pdfs(
        tex_path.with_suffix(".pdf"),
        artifact_student_pdfs_dir(ctx.artifact_dir),
        artifact_class_report_combined_portrait_pdf_path(ctx.artifact_dir),
        suffix="portrait",
    )

    class_pdf_path = tex_path.with_suffix(".pdf")
    class_2up_path = artifact_class_report_pdf_2up_path(ctx.artifact_dir)
    if class_pdf_path.is_file():
        make_2up_landscape_pdf(class_pdf_path, class_2up_path)
    if class_2up_path.is_file():
        _merge_pdfs(
            class_2up_path,
            artifact_student_pdfs_dir(ctx.artifact_dir),
            artifact_class_report_combined_portrait_2up_pdf_path(ctx.artifact_dir),
            suffix="portrait_2up",
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Side-channel review queue
# ---------------------------------------------------------------------------

def _write_review_queue(
    full_reports: dict[str, dict],
    artifact_dir: Path,
    collisions: list[dict] | None = None,
) -> int:
    """Emit a standalone list of marks the AI flagged as medium/low confidence.

    Returns the number of flagged confidence entries (also written to the
    JSON's ``"total"``). Cross-page mark collisions, if any, are appended to
    the same artifacts under ``"collisions"`` / ``"collisions_total"``.

    Pure side artifact: read by humans only, never by any pipeline step.
    Existing student/class reports and PDFs are unaffected by this code path.

    Each entry in the JSON file:
        {
          "student": ..., "question": ..., "confidence": "medium" | "low",
          "assigned_marks": ..., "max_marks": ...,
          "student_answer": ..., "correct_answer": ...,
          "explanation": ...    # truncated to ~200 chars for readability
        }

    Empty / missing confidence is treated as ``"high"`` and excluded.
    """
    import json

    entries: list[dict] = []
    for student_name in sorted(full_reports):
        report = full_reports[student_name]
        for q in report.get("questions") or []:
            conf = (q.get("confidence") or "").strip().lower()
            if conf in ("", "high"):
                continue
            if conf not in ("medium", "low"):
                # Unknown values still surface — they're an AI mistake worth seeing.
                pass
            explanation = str(q.get("explanation") or "")
            if len(explanation) > 200:
                explanation = explanation[:200].rstrip() + "…"
            entries.append({
                "student":        student_name,
                "question":       str(q.get("number", "")),
                "confidence":     conf,
                "assigned_marks": q.get("assigned_marks"),
                "max_marks":      q.get("max_marks"),
                "student_answer": str(q.get("student_answer") or ""),
                "correct_answer": str(q.get("correct_answer") or ""),
                "explanation":    explanation,
            })

    # Sort: low first, then medium; within each, by student then question.
    _conf_rank = {"low": 0, "medium": 1}
    entries.sort(key=lambda e: (_conf_rank.get(e["confidence"], 2), e["student"], e["question"]))

    coll = list(collisions or [])
    coll.sort(key=lambda c: (c.get("student", ""), str(c.get("question", "")), c.get("page", 0)))

    json_path = artifact_review_queue_json_path(artifact_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps({
            "entries":          entries,
            "total":            len(entries),
            "collisions":       coll,
            "collisions_total": len(coll),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Markdown mirror — quick to skim.
    md_lines = [
        "# Review Queue",
        "",
        f"**{len(entries)} marks flagged for human review** "
        "(medium or low confidence; no impact on the marks already awarded).",
        "",
    ]
    if entries:
        md_lines += [
            "| Conf | Student | Q | Awarded | Max | Student Answer | Correct | Explanation |",
            "|------|---------|---|---------|-----|----------------|---------|-------------|",
        ]
        for e in entries:
            sa = (e["student_answer"] or "").replace("|", "/").replace("\n", " ")
            ca = (e["correct_answer"] or "").replace("|", "/")
            ex = (e["explanation"] or "").replace("|", "/").replace("\n", " ")
            am = e["assigned_marks"]
            am_s = "?" if am is None else str(am)
            md_lines.append(
                f"| {e['confidence']} | {e['student']} | {e['question']} | {am_s} | "
                f"{e['max_marks']} | {sa} | {ca} | {ex} |"
            )
    else:
        md_lines.append("*No medium/low-confidence entries — the AI was confident on every question.*")

    if coll:
        md_lines += [
            "",
            "## Cross-page collisions",
            "",
            f"**{len(coll)} cross-page mark collision(s)** — same question scored on multiple pages.",
            "",
            "| Student | Q | Page | Earlier | Page | Winner |",
            "|---------|---|------|---------|------|--------|",
        ]
        for c in coll:
            md_lines.append(
                f"| {c['student']} | {c['question']} | {c['page']} | "
                f"{c['earlier_marks']} | {c['page_marks']} | {c['winner']} |"
            )

    artifact_review_queue_md_path(artifact_dir).write_text(
        "\n".join(md_lines) + "\n", encoding="utf-8"
    )

    return len(entries)


# ---------------------------------------------------------------------------
# Step 25 — Per-student reports (XML + MD)
# ---------------------------------------------------------------------------

def step_25_per_student_reports(ctx: Any) -> None:
    """Merge per-page marking results into per-student XML + MD reports.

    Populates ``ctx.student_summaries``, ``ctx.full_reports``, ``ctx.q_totals``
    for downstream steps (26–29) to consume.

    Honours ``--student`` CLI filter (lower-case exact match): downstream
    class-report step (28) is skipped when the filter is active because a
    one-or-two-student "class" average would be misleading.
    """
    fmt = get_marking_format()
    total_max_marks = ctx.scaffold.total_marks
    correct_answers, marking_criteria_by_num = _build_answer_lookup(ctx)
    names = _derive_student_names(ctx.artifact_dir, fmt=fmt)
    workers = int(os.environ.get("REPORT_COMPILE_WORKERS", os.environ.get("MARKING_WORKERS", "4")))

    cli_student_filter = getattr(ctx, "student_filter", None)
    if cli_student_filter:
        wanted = set(cli_student_filter)
        names = [n for n in names if n.strip().lower() in wanted]

    ctx.artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_marking_students_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    artifact_student_reports_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)

    student_summaries, full_reports, q_totals, failed, collisions = _pass1_merge_students(
        ctx, fmt, names, total_max_marks, correct_answers, marking_criteria_by_num, workers
    )
    ctx.student_summaries = student_summaries
    ctx.full_reports = full_reports
    ctx.q_totals = q_totals
    ctx.failed_students = failed
    ctx.mark_collisions = collisions


# ---------------------------------------------------------------------------
# Step 26 — Class statistics + grade curve
# ---------------------------------------------------------------------------

def step_26_class_stats_curve(ctx: Any) -> None:
    """Compute class average + grade-curve offset.

    Target priority: prompt override ``ctx.instruction.curved_grade_override``
    → env var ``GRADE_CURVE_TARGET`` (default 80). Mutates
    ``ctx.student_summaries`` in place to add ``curved_pct``, and writes a
    small ``class_stats.json`` artifact recording the resolved target/offset
    so steps 27 and 28 use identical numbers.
    """
    summaries = ctx.student_summaries
    target = _effective_curve_target(ctx)
    _apply_grade_curve(summaries, target)
    known = [s["percentage"] for s in summaries if s["percentage"] is not None]
    class_avg = int(round(sum(known) / len(known))) if known else None
    curve_offset = (target - class_avg) if class_avg is not None else 0
    p = artifact_class_stats_json_path(ctx.artifact_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({
            "class_average_pct": class_avg,
            "curve_offset": curve_offset,
            "curve_target": target,
            "n_students": len(summaries),
            "n_with_marks": len(known),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Step 27 — Per-student PDFs
# ---------------------------------------------------------------------------

def step_27_per_student_pdfs(ctx: Any) -> None:
    """Write per-student .tex files then compile them in parallel via xelatex.

    Visibility of the curved % in each per-student PDF header follows
    ``ctx.instruction.curved_grade_visible`` first, then env var
    ``CURVED_GRADE_VISIBLE`` (default true).
    """
    exam_name = ctx.artifact_dir.parent.name
    workers = int(os.environ.get("REPORT_COMPILE_WORKERS", os.environ.get("MARKING_WORKERS", "4")))
    show_curved_grade = _curved_grade_visible(ctx)
    artifact_student_pdfs_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    _pass2_write_tex(
        ctx.student_summaries, ctx.full_reports, ctx.artifact_dir, exam_name, workers,
        show_curved_grade=show_curved_grade,
    )


# ---------------------------------------------------------------------------
# Step 28 — Class report
# ---------------------------------------------------------------------------

def step_28_class_report(ctx: Any) -> str:
    """Build & write class XML/MD/TeX/PDF + concat combined PDF.

    Returns a discriminator so the wrapper can pick the right summary line:
    ``"done"`` (work ran), ``"skipped_filter"`` (``--student`` filter active —
    a warning was already emitted), ``"skipped_empty"`` (no per-student
    summaries to compile).
    """
    cli_student_filter = getattr(ctx, "student_filter", None)
    if cli_student_filter:
        from xscore.shared.terminal_ui import warn_line
        warn_line(
            "--student filter active — skipping class report (would not be "
            "representative of the full class)."
        )
        return "skipped_filter"
    if not ctx.student_summaries:
        return "skipped_empty"
    exam_name = ctx.artifact_dir.parent.name
    artifact_class_report_dir(ctx.artifact_dir).mkdir(parents=True, exist_ok=True)
    _build_class_report(ctx, ctx.student_summaries, ctx.q_totals, exam_name)
    return "done"


# ---------------------------------------------------------------------------
# Step 29 — Review queue
# ---------------------------------------------------------------------------

def step_29_review_queue(ctx: Any) -> int:
    """Emit the side-channel review queue (medium / low confidence marks + collisions).

    Always runs, even when no entries are flagged, so downstream tooling can
    rely on the artifact existing. Returns the count of flagged entries.
    Cross-page mark collisions captured by step 25 are appended to the same
    artifact under ``"collisions"``.
    """
    return _write_review_queue(
        ctx.full_reports, ctx.artifact_dir,
        collisions=getattr(ctx, "mark_collisions", None) or None,
    )


# ---------------------------------------------------------------------------
# Backward-compat shim: still callable by anything that hasn't migrated.
# ---------------------------------------------------------------------------

def compile_reports(ctx: Any) -> list[dict]:
    """Run steps 25–29 in sequence (kept for callers not yet migrated).

    Returns a list of per-student summary dicts (keys: name, total_marks, percentage).
    """
    step_25_per_student_reports(ctx)
    step_26_class_stats_curve(ctx)
    step_27_per_student_pdfs(ctx)
    step_28_class_report(ctx)
    step_29_review_queue(ctx)
    return ctx.student_summaries or []


def load_student_results_from_reports(artifact_dir: Path) -> list:
    """Read all student report XML files and reconstruct StudentResult objects."""
    from xscore.shared.models import StudentResult

    results = []
    failed: list[str] = []
    for f in sorted(artifact_reports_students_dir(artifact_dir).glob("*.xml")):
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
