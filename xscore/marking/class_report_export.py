"""Side-channel artifact writers for the class report.

Two pure writers extracted from ``class_report.py``:

- :func:`_write_class_marks_xlsx` — per-student × per-question marks grid as
  ``class_marks.xlsx`` (called inline by ``_build_class_report``).
- :func:`_write_review_queue` — confidence audit of every marked question
  (sorted by ascending confidence) plus any cross-page collisions, as JSON
  + Markdown + plain text (called by ``merge_reports.build_review_queue``).

Both take their data as parameters and have no compile-time dependency on
``class_report``; ``class_report`` and ``merge_reports`` import them from
here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xscore.shared.exam_paths import (
    artifact_class_marks_xlsx_path,
    artifact_review_queue_json_path,
    artifact_review_queue_md_path,
    artifact_review_queue_txt_path,
)


def _write_class_marks_xlsx(
    *,
    class_report: dict,
    full_reports: dict[str, dict],
    scaffold_questions: list,
    out_path: Path,
) -> None:
    """Write a per-student × per-question marks grid as ``class_marks.xlsx``.

    One column per scaffold node (parents *and* leaves) in DFS order, plus
    Total / Raw % / Curved %. Parent columns roll up to the sum of their leaf
    descendants so a row's Total equals the sum of any complete level of the
    tree. Headers, max-marks row, and a class-average row at the bottom.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill

    from xscore.shared.models import flatten_questions, gradable_questions

    # Walk scaffold once. Column key matches per_question_max_marks keys
    # (same _N duplicate suffixing). For both parents and leaves,
    # gradable_questions([q]) returns the leaf set used for rollup.
    seen: dict[str, int] = {}
    columns: list[tuple[str, list[str]]] = []   # (column_key, leaf_keys_for_rollup)
    for q in flatten_questions(scaffold_questions):
        num = str(q.number or "")
        if not num:
            continue
        seen[num] = seen.get(num, 0) + 1
        key = num if seen[num] == 1 else f"{num}_{seen[num]}"
        leaf_keys = [str(c.number or "") for c in gradable_questions([q])]
        columns.append((key, leaf_keys))

    students = class_report["students"]
    max_marks = class_report["per_question_max_marks"]
    avgs = class_report["per_question_averages"]
    total_max = class_report["total_max_marks"]
    class_pct = class_report["class_average_pct"]

    wb = Workbook()
    ws = wb.active
    ws.title = "Class marks"

    ws.append(["Student"] + [k for k, _ in columns] + ["Total", "Raw %", "Curved %"])
    ws.append(
        ["Max marks"]
        + [max_marks.get(k, "") for k, _ in columns]
        + [total_max, None, None]
    )

    def _sum_leaves(report: dict, leaf_keys: list[str]) -> float | None:
        by_num = {q["number"]: q.get("assigned_marks") for q in report.get("questions", [])}
        vals = [by_num.get(k) for k in leaf_keys]
        nums = [v for v in vals if v is not None]
        return sum(nums) if nums else None

    for s in students:
        report = full_reports.get(s["name"], {})
        row: list = [s["name"]]
        for _key, leaf_keys in columns:
            row.append(_sum_leaves(report, leaf_keys))
        row += [
            s.get("total_marks"),
            s["percentage"] / 100 if s.get("percentage") is not None else None,
            s["curved_pct"] / 100 if s.get("curved_pct") is not None else None,
        ]
        ws.append(row)

    # Class-average row — per_question_averages already covers parents
    # (subtree sums) so the row is internally consistent. Total is the
    # mean of known student totals to avoid double-counting parent rollups.
    known_totals = [s["total_marks"] for s in students if s.get("total_marks") is not None]
    avg_total = round(sum(known_totals) / len(known_totals), 1) if known_totals else None
    ws.append(
        ["Class average"]
        + [avgs.get(k, None) for k, _ in columns]
        + [avg_total, class_pct / 100 if class_pct is not None else None, None]
    )

    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="EEEEEE")
    for cell in list(ws[1]) + list(ws[2]) + list(ws[ws.max_row]):
        cell.font = bold
        cell.fill = head_fill
    ws.freeze_panes = "B3"

    name_col_w = max(12, max((len(s["name"]) for s in students), default=10) + 2)
    ws.column_dimensions["A"].width = name_col_w
    for col_idx in range(2, 2 + len(columns)):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = 6
    for offset, w in enumerate([10, 8, 8]):  # Total, Raw %, Curved %
        ws.column_dimensions[
            ws.cell(row=1, column=2 + len(columns) + offset).column_letter
        ].width = w

    raw_col = 2 + len(columns) + 1
    curve_col = 2 + len(columns) + 2
    for r in range(2, ws.max_row + 1):
        ws.cell(row=r, column=raw_col).number_format = "0%"
        ws.cell(row=r, column=curve_col).number_format = "0%"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


def _qnum_natural_key(qnum: str) -> tuple:
    """Sort '2' before '10', and 'Q_2' suffix after its base."""
    base, _, suffix = qnum.partition("_")
    try:
        return (0, int(base), suffix)
    except ValueError:
        return (1, qnum, "")


def format_review_entry_line(entry: dict) -> str:
    """Format one queue entry as a single line for terminal echo / review.txt.

    Layout: ``{student}  Q{qnum}  (p.{page})  conf={int}  · {problem}``.
    The trailing ``· {problem}`` segment is omitted when ``problem`` is empty.
    """
    student = entry["student"]
    qnum = entry["question"]
    page = entry.get("page")
    conf = entry["confidence"]
    problem = entry.get("problem") or ""
    page_str = f"(p.{page})" if page is not None else "(p.?)"
    base = f"{student}  Q{qnum}  {page_str}  conf={conf}"
    if problem:
        return f"{base}  · {problem}"
    return base


def _write_review_queue(
    full_reports: dict[str, dict],
    artifact_dir: Path,
    collisions: list[dict] | None = None,
    page_assignments: list[Any] | None = None,
) -> list[dict]:
    """Emit confidence-audit artifacts for every marked question.

    Writes three sibling files in ``33_review_queue/``:

    - ``review.json`` — structured entries, ordered by ascending confidence,
      plus the cross-page ``collisions`` section unchanged.
    - ``review.md``   — human-readable markdown table, same order.
    - ``review.txt``  — plain-text per-entry pretty format (one line per
      question), same order; mirrors what the terminal echoes for the top N.

    Returns the entries list so the caller can echo the lowest-confidence
    rows to the terminal without rebuilding it. Pure side artifact: read by
    humans only, never by any pipeline step.

    Each JSON entry:
        {
          "student": ..., "question": ..., "confidence": <int 0..10>,
          "assigned_marks": ..., "max_marks": ...,
          "student_answer": ..., "correct_answer": ...,
          "explanation": ...,    # truncated to ~200 chars
          "problem":     ...,    # may be empty string
          "page":        <int|None>  # absolute scan page, when known
        }
    """
    student_to_pages: dict[str, list[int]] = {
        a.student_name: list(a.page_numbers) for a in (page_assignments or [])
    }

    entries: list[dict] = []
    for student_name in full_reports:
        report = full_reports[student_name]
        pages = student_to_pages.get(student_name, [])
        for q in report.get("questions") or []:
            am = q.get("assigned_marks")
            if am is None:
                continue  # question was not marked — exclude from audit
            cf = q.get("confidence")
            try:
                conf_int = int(cf) if cf is not None else 5
            except (TypeError, ValueError):
                conf_int = 5
            if conf_int < 0:
                conf_int = 0
            elif conf_int > 10:
                conf_int = 10

            p_label = q.get("page_label")
            scan_page: int | None = None
            if isinstance(p_label, int) and 1 <= p_label <= len(pages):
                scan_page = pages[p_label - 1]

            explanation = str(q.get("explanation") or "")
            if len(explanation) > 200:
                explanation = explanation[:200].rstrip() + "…"

            entries.append({
                "student":        student_name,
                "question":       str(q.get("number", "")),
                "confidence":     conf_int,
                "problem":        str(q.get("problem") or "").strip(),
                "assigned_marks": am,
                "max_marks":      q.get("max_marks"),
                "student_answer": str(q.get("student_answer") or ""),
                "correct_answer": str(q.get("correct_answer") or ""),
                "explanation":    explanation,
                "page":           scan_page,
            })

    # Sort: confidence ascending, then student, then question (natural).
    entries.sort(key=lambda e: (
        e["confidence"], e["student"], _qnum_natural_key(e["question"]),
    ))

    coll = list(collisions or [])
    coll.sort(key=lambda c: (c.get("student", ""), str(c.get("question", "")), c.get("page", 0)))

    below_7 = sum(1 for e in entries if e["confidence"] < 7)

    # JSON artifact
    json_path = artifact_review_queue_json_path(artifact_dir)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps({
            "entries":          entries,
            "total":            len(entries),
            "below_7_total":    below_7,
            "collisions":       coll,
            "collisions_total": len(coll),
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Markdown artifact — quick to skim, sorted top-down by ascending confidence.
    md_lines = [
        "# Review Queue",
        "",
        f"**Marking confidence audit** — all {len(entries)} questions sorted "
        f"by confidence (lowest first). {below_7} entries have confidence "
        "&lt; 7. No impact on the marks already awarded.",
        "",
    ]
    if entries:
        md_lines += [
            "| Conf | Student | Q | Awarded | Max | Student Answer | Correct | Problem | Explanation |",
            "|------|---------|---|---------|-----|----------------|---------|---------|-------------|",
        ]
        for e in entries:
            sa = (e["student_answer"] or "").replace("|", "/").replace("\n", " ")
            ca = str(e["correct_answer"] or "").replace("|", "/")
            ex = (e["explanation"] or "").replace("|", "/").replace("\n", " ")
            problem = (e["problem"] or "").replace("|", "/").replace("\n", " ")
            if len(problem) > 120:
                problem = problem[:120].rstrip() + "…"
            am = e["assigned_marks"]
            am_s = "?" if am is None else str(am)
            md_lines.append(
                f"| {e['confidence']} | {e['student']} | {e['question']} | {am_s} | "
                f"{e['max_marks']} | {sa} | {ca} | {problem} | {ex} |"
            )
    else:
        md_lines.append("*No marked questions to audit.*")

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

    # Plain-text artifact — one line per entry, byte-identical to terminal echo.
    txt_lines = [format_review_entry_line(e) for e in entries]
    artifact_review_queue_txt_path(artifact_dir).write_text(
        "\n".join(txt_lines) + ("\n" if txt_lines else ""), encoding="utf-8",
    )

    return entries
