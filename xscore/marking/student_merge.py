"""Per-student page merging: collisions, name discovery, answer-key lookup,
parallel pass-1 that writes per-student XML + Markdown reports.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from xscore.marking.report_markdown import _fmt_pct, _student_report_to_md
from xscore.marking.report_xml import student_report_to_xml
from xscore.shared.exam_paths import (
    artifact_blueprint_path,
    artifact_marked_path,
    artifact_marking_students_dir,
    artifact_student_report_dir,
    artifact_student_report_md_full_path,
    artifact_student_report_md_path,
    artifact_student_report_xml_full_path,
    artifact_student_report_xml_path,
    safe_student_name as _safe_name,
)
from xscore.shared.terminal_ui import ok_line, warn_line


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


def _augment_with_unanswered(
    filtered_report: dict,
    artifact_dir: Path,
    register: dict,
    fmt: Any,
    correct_answers: dict[str, str],
    marking_criteria_by_num: dict[str, str],
) -> dict | None:
    """Return a copy of *filtered_report* with one extra row per question on
    every scan page the student left blank, sourced from the step-24 blueprint.

    Each injected row carries ``student_answer=""``, ``assigned_marks=0`` and
    ``_unanswered=True``. The flag drives a different rendering branch in
    ``report_latex.py`` and ``report_markdown.py`` (``(not answered)`` italic).

    Returns ``None`` when there's nothing to add (no skipped scan pages, no
    matching blueprints, or every blueprint question is already in the
    filtered report). The caller treats ``None`` as "no _full batch needed
    for this student".
    """
    from xscore.marking.marking_page_register import _cover_offset

    student_name = filtered_report["student_name"]
    student_record = next(
        (s for s in register.get("students") or [] if s.get("student_name") == student_name),
        None,
    )
    if student_record is None:
        return None

    skipped_scan_pages = list(student_record.get("skipped_scan_pages") or [])
    if not skipped_scan_pages:
        return None

    cover_page_number = student_record.get("cover_page_number")
    has_cover = cover_page_number is not None
    page_numbers: list[int] = list(student_record.get("page_numbers") or [])
    empty_exam_has_cover = bool(
        (register.get("metadata") or {}).get("empty_exam_has_cover", False)
    )
    cover_offset = _cover_offset(has_cover, empty_exam_has_cover)

    # Track every qnum already represented so the injected rows don't double
    # up against the marked report or against another skipped page that
    # mentions the same cross-page question.
    existing_qnums: set[str] = {
        str(q.get("number", "")) for q in filtered_report.get("questions") or []
    }

    augmented_questions = list(filtered_report.get("questions") or [])
    appended = 0
    for scan_page in sorted(skipped_scan_pages):
        try:
            p_label = page_numbers.index(scan_page) + 1
        except ValueError:
            continue   # skipped page not in this student's roster — skip
        if has_cover and p_label == 1:
            continue   # student cover page never has questions
        answer_label = p_label - cover_offset
        if answer_label < 1:
            continue
        bp_path = artifact_blueprint_path(
            artifact_dir, answer_label, fmt=fmt.artifact_ext()
        )
        if not bp_path.is_file():
            continue   # blank-in-empty page — no scaffold questions to inject
        try:
            blueprint = fmt.deserialize_blueprint(bp_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for q in blueprint.get("questions") or []:
            qnum = str(q.get("number", ""))
            if qnum in existing_qnums:
                continue
            entry = q.copy()
            entry["student_answer"] = ""
            entry["assigned_marks"] = 0
            entry["_unanswered"] = True
            entry["page_label"] = p_label
            entry["correct_answer"] = correct_answers.get(qnum, entry.get("correct_answer", ""))
            entry["marking_criteria"] = marking_criteria_by_num.get(
                qnum, entry.get("marking_criteria", "")
            )
            augmented_questions.append(entry)
            existing_qnums.add(qnum)
            appended += 1

    if appended == 0:
        return None

    # Stable sort by page_label so unanswered rows from page P slot in among
    # marked rows from page P. Marked rows already arrive in page order from
    # the existing merge loop; in-page question order is preserved by stable sort.
    augmented_questions.sort(key=lambda q: q.get("page_label") or 0)

    augmented = dict(filtered_report)
    augmented["questions"] = augmented_questions
    return augmented


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
) -> tuple[list[dict], dict[str, dict], dict[str, dict], dict[str, list[float]], list[dict], list[dict]]:
    """Parallel: merge per-page marks, write XML + MD per student, accumulate q_totals.

    Returns ``(student_summaries, full_reports, full_reports_augmented,
    q_totals, failed, collisions)``. ``full_reports_augmented`` only contains
    students whose augmented report differs from the filtered one (i.e. they
    have at least one skipped scan page that the marking page register can
    map to a blueprint with questions).

    Per-student failures are collected into ``failed`` and the run continues
    — one bad student does not block the rest. ``collisions`` records
    cross-page mark conflicts for the review queue.
    """
    from xscore.marking.marking_page_register import load_register

    student_summaries: list[dict] = []
    full_reports: dict[str, dict] = {}
    full_reports_augmented: dict[str, dict] = {}
    q_totals: dict[str, list[float]] = {}
    collisions: list[dict] = []
    _summaries_lock = threading.Lock()
    _q_totals_lock = threading.Lock()
    _collisions_lock = threading.Lock()

    # Load the marking page register once. Used by _augment_with_unanswered
    # to find each student's skipped_scan_pages. None when resuming from a
    # run that predates the register artifact — augmentation is skipped.
    register = load_register(ctx.artifact_dir)

    def _process_one(name: str) -> None:
        report = _merge_student_pages(
            ctx.artifact_dir, name, ctx.pages_per_student, total_max_marks, fmt=fmt,
            collisions=collisions, collisions_lock=_collisions_lock,
        )
        for q in report["questions"]:
            q["correct_answer"] = correct_answers.get(str(q.get("number", "")), "")
            q["marking_criteria"] = marking_criteria_by_num.get(str(q.get("number", "")), "")

        artifact_student_report_dir(ctx.artifact_dir, name).mkdir(parents=True, exist_ok=True)
        artifact_student_report_xml_path(ctx.artifact_dir, name).write_text(
            student_report_to_xml(report), encoding="utf-8"
        )
        artifact_student_report_md_path(ctx.artifact_dir, name).write_text(
            _student_report_to_md(report), encoding="utf-8"
        )

        # q_totals: filtered only — augmented rows have assigned_marks=0
        # and would skew the per-question class averages used by step 27/29.
        with _q_totals_lock:
            for q in report["questions"]:
                am = q.get("assigned_marks")
                if am is not None:
                    q_totals.setdefault(str(q.get("number", "")), []).append(float(am))

        # Augmented variant — only when the register knows about this run
        # AND the student has unanswered questions to inject.
        aug = None
        if register is not None:
            aug = _augment_with_unanswered(
                report, ctx.artifact_dir, register, fmt,
                correct_answers, marking_criteria_by_num,
            )
            if aug is not None:
                artifact_student_report_xml_full_path(ctx.artifact_dir, name).write_text(
                    student_report_to_xml(aug), encoding="utf-8"
                )
                artifact_student_report_md_full_path(ctx.artifact_dir, name).write_text(
                    _student_report_to_md(aug), encoding="utf-8"
                )

        with _summaries_lock:
            student_summaries.append({
                "name": name,
                "total_marks": report["total_marks"],
                "percentage": report["percentage"],
            })
            full_reports[name] = report
            if aug is not None:
                full_reports_augmented[name] = aug

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

    return (
        student_summaries, full_reports, full_reports_augmented,
        q_totals, failed, collisions,
    )
