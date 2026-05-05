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
    curve_target_pct: int | None = None,
) -> None:
    """Write a per-student × per-question marks grid as ``class_marks.xlsx``.

    Cells are written as **live formulas** so the teacher can edit a leaf
    mark in Excel and have the totals, percentages, and curved grades
    recalculate. Layout:

    - **Curve block** at rows 1–2: ``Curve target`` (editable, B1) and
      ``Curve offset`` (computed, B2). Per-student Curved % cells reference
      ``$B$2`` so changes to the target propagate.
    - **Table A** (every scaffold node, parents + leaves) and **Table B**
      (top-level only). Each: header / max-marks / per-student rows /
      class-average, with Total / Raw % / Curved % on the right.
    - Leaf cells hold plain numbers (the editable inputs). Parent cells in
      Table A are ``=SUM(<leaves under that parent>)``. Total / Raw % /
      Curved % and the class-average aggregates are formulas too.

    ``curve_target_pct`` is the integer 0–100 (typically read from
    ``class_stats.json``); falls back to env ``GRADE_CURVE_TARGET``, then 80
    — matches :func:`xscore.marking.class_report._grade_curve_target`.
    """
    import os

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    from xscore.shared.models import flatten_questions, gradable_questions

    def _build_columns(qs: list) -> list[tuple[str, list[str], list[int], bool]]:
        # Each tuple: (display_key with _N suffix for duplicate numbers,
        # leaf_raw_nums = subtree leaf numbers for full-report rollup lookup,
        # leaf_col_indices = subtree leaf column indices in this table for
        # SUM formulas, is_scaffold_leaf flag). Identity-based id() lookup so
        # duplicate question numbers don't cross-link parent rollups.
        seen: dict[str, int] = {}
        keys: list[str] = []
        questions: list = []
        id_to_idx: dict[int, int] = {}
        for q in qs:
            num = str(q.number or "")
            if not num:
                continue
            seen[num] = seen.get(num, 0) + 1
            keys.append(num if seen[num] == 1 else f"{num}_{seen[num]}")
            questions.append(q)
            id_to_idx[id(q)] = len(keys) - 1
        cols: list[tuple[str, list[str], list[int], bool]] = []
        for key, q in zip(keys, questions):
            leaves = gradable_questions([q])
            is_scaffold_leaf = len(leaves) == 1 and leaves[0] is q
            leaf_raw_nums = [str(c.number or "") for c in leaves]
            leaf_col_indices = [id_to_idx[id(c)] for c in leaves if id(c) in id_to_idx]
            cols.append((key, leaf_raw_nums, leaf_col_indices, is_scaffold_leaf))
        return cols

    cols_all = _build_columns(flatten_questions(scaffold_questions))
    cols_top = _build_columns(scaffold_questions)

    students = class_report["students"]
    max_marks = class_report["per_question_max_marks"]
    avgs = class_report["per_question_averages"]
    total_max = class_report["total_max_marks"]

    # Resolve curve target: explicit arg → env → default 80.
    if curve_target_pct is None:
        env_val = os.environ.get("GRADE_CURVE_TARGET", "")
        try:
            curve_target_pct = int(env_val) if env_val else 80
        except ValueError:
            curve_target_pct = 80
    curve_target_pct = max(0, min(100, int(curve_target_pct)))

    def _sum_leaves(report: dict, leaf_raw_nums: list[str]) -> float | None:
        # Static rollup: sum of subtree-leaf marks looked up in the full
        # student report. Used for Table B (top-level rollups) and for
        # leaf cells (single-element list).
        by_num = {q.get("number"): q.get("assigned_marks") for q in report.get("questions", [])}
        nums = [v for v in (by_num.get(k) for k in leaf_raw_nums) if v is not None]
        return sum(nums) if nums else None

    wb = Workbook()
    ws = wb.active
    ws.title = "Class marks"

    bold = Font(bold=True)
    head_fill = PatternFill("solid", fgColor="EEEEEE")

    # ------------------------------------------------------------------
    # Curve block (rows 1–2). B2 is filled in after Table A rows are
    # known so the AVERAGE range is correct.
    # ------------------------------------------------------------------
    ws.cell(row=1, column=1, value="Curve target").font = bold
    ws.cell(row=1, column=2, value=curve_target_pct / 100).number_format = "0%"
    ws.cell(row=2, column=1, value="Curve offset").font = bold
    ws.cell(row=2, column=2).number_format = "0%"  # formula filled below

    next_row = 4  # leave row 3 blank as a visual separator

    def _append_table(
        columns: list[tuple[str, list[str], list[int], bool]],
        start_row: int,
    ) -> tuple[int, int, int]:
        """Write header, max-marks, per-student, class-average rows starting
        at ``start_row``. Returns ``(header_row, class_avg_row, total_col_idx)``;
        the % columns sit at ``total_col_idx + 1`` and ``+ 2``.
        """
        n_q = len(columns)
        total_col = 2 + n_q  # 1 = name, then n_q question cols
        raw_col = total_col + 1
        curved_col = total_col + 2

        header_row = start_row
        max_marks_row = start_row + 1
        student_first = start_row + 2
        student_last = student_first + len(students) - 1
        class_avg_row = student_last + 1

        # Per-question column letters (indexed 0..n_q-1) and the trailing trio.
        q_letter = [get_column_letter(2 + i) for i in range(n_q)]
        total_letter = get_column_letter(total_col)
        raw_letter = get_column_letter(raw_col)
        curved_letter = get_column_letter(curved_col)
        max_total_ref = f"${total_letter}${max_marks_row}"

        # Static columns hold the editable / authoritative numbers for this
        # table — Total sums these. In Table A: scaffold leaves only. In
        # Table B: every column (top-level rollup, leaves not present here).
        static_indices = [
            i for i, (_, _, leaf_col_indices, is_scaffold_leaf) in enumerate(columns)
            if is_scaffold_leaf or not leaf_col_indices
        ]

        # Header row.
        for c, val in enumerate(
            ["Student"] + [k for k, _, _, _ in columns] + ["Total", "Raw %", "Curved %"],
            start=1,
        ):
            ws.cell(row=header_row, column=c, value=val)

        # Max marks row — per-question max marks, then total_max literal.
        ws.cell(row=max_marks_row, column=1, value="Max marks")
        for i, (key, _, _, _) in enumerate(columns):
            ws.cell(row=max_marks_row, column=2 + i, value=max_marks.get(key, ""))
        ws.cell(row=max_marks_row, column=total_col, value=total_max)

        # Per-student rows.
        for s_idx, s in enumerate(students):
            r = student_first + s_idx
            report = full_reports.get(s["name"], {})
            ws.cell(row=r, column=1, value=s["name"])

            for i, (_, leaf_raw_nums, leaf_col_indices, is_scaffold_leaf) in enumerate(columns):
                cell = ws.cell(row=r, column=2 + i)
                if is_scaffold_leaf or not leaf_col_indices:
                    # Static value — leaf mark or top-level rollup from full
                    # report. Editable; Total formula sums these.
                    cell.value = _sum_leaves(report, leaf_raw_nums)
                else:
                    # Parent with all leaves present in this table — formula
                    # SUM over those leaf cells. Updates when a leaf is edited.
                    refs = ",".join(f"{q_letter[j]}{r}" for j in leaf_col_indices)
                    cell.value = f"=SUM({refs})"

            # Total: SUM of this row's static-value cells.
            if static_indices:
                refs = ",".join(f"{q_letter[j]}{r}" for j in static_indices)
                ws.cell(row=r, column=total_col, value=f"=SUM({refs})")
            else:
                ws.cell(row=r, column=total_col, value=0)

            # Raw % = Total / MaxTotal.
            ws.cell(
                row=r, column=raw_col,
                value=f"={total_letter}{r}/{max_total_ref}",
            ).number_format = "0%"
            # Curved % = MAX(0, MIN(1, Raw + offset)).
            ws.cell(
                row=r, column=curved_col,
                value=f"=MAX(0,MIN(1,{raw_letter}{r}+$B$2))",
            ).number_format = "0%"

        # Class-average row.
        ws.cell(row=class_avg_row, column=1, value="Class average")
        for i, (key, _, _, _) in enumerate(columns):
            ws.cell(row=class_avg_row, column=2 + i, value=avgs.get(key, None))
        ws.cell(
            row=class_avg_row, column=total_col,
            value=f"=AVERAGE({total_letter}{student_first}:{total_letter}{student_last})",
        )
        ws.cell(
            row=class_avg_row, column=raw_col,
            value=f"=AVERAGE({raw_letter}{student_first}:{raw_letter}{student_last})",
        ).number_format = "0%"
        ws.cell(
            row=class_avg_row, column=curved_col,
            value=f"=AVERAGE({curved_letter}{student_first}:{curved_letter}{student_last})",
        ).number_format = "0%"

        return header_row, class_avg_row, total_col

    top_a, bot_a, total_a = _append_table(cols_all, next_row)

    # Fill in the curve-offset formula now that we know Table A's student
    # row range. Both tables share this offset since they list the same
    # students, so we anchor on Table A's Raw % column.
    n = len(students)
    if n > 0:
        raw_letter_a = get_column_letter(total_a + 1)
        student_first_a = top_a + 2
        student_last_a = student_first_a + n - 1
        ws.cell(
            row=2, column=2,
            value=f"=B1-AVERAGE({raw_letter_a}{student_first_a}:{raw_letter_a}{student_last_a})",
        )
    else:
        ws.cell(row=2, column=2, value=0)

    # Two blank rows + bold heading, then Table B.
    heading_row = bot_a + 3
    ws.cell(row=heading_row, column=1, value="Top-level questions").font = bold
    top_b, bot_b, total_b = _append_table(cols_top, heading_row + 2)

    # Bold + grey fill on header / max-marks / class-average rows.
    for top, bot in [(top_a, bot_a), (top_b, bot_b)]:
        for row_idx in (top, top + 1, bot):
            for cell in ws[row_idx]:
                cell.font = bold
                cell.fill = head_fill

    ws.freeze_panes = "B6"

    # Column widths — coalesce by column letter so Table B's Total / % cells
    # (which fall under column letters that hold question cells in Table A)
    # get the wider width.
    name_col_w = max(12, max((len(s["name"]) for s in students), default=10) + 2)
    widths_by_letter: dict[str, float] = {}

    def _want(col_idx: int, w: float) -> None:
        letter = get_column_letter(col_idx)
        widths_by_letter[letter] = max(widths_by_letter.get(letter, 0), w)

    _want(1, name_col_w)
    for cols, total_col in [(cols_all, total_a), (cols_top, total_b)]:
        for i in range(len(cols)):
            _want(2 + i, 6)
        for offset, w in enumerate([10, 8, 8]):  # Total, Raw %, Curved %
            _want(total_col + offset, w)
    for letter, w in widths_by_letter.items():
        ws.column_dimensions[letter].width = w

    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out_path)


def _qnum_natural_key(qnum: str) -> tuple:
    """Sort '2' before '10', and 'Q_2' suffix after its base."""
    base, _, suffix = qnum.partition("_")
    try:
        return (0, int(base), suffix)
    except ValueError:
        return (1, qnum, "")


def format_review_entry_lines(entries: list[dict]) -> list[str]:
    """Format queue entries as one aligned line each, for terminal echo /
    ``review.txt``.

    Layout per row: ``{student}  Q{qnum}  (p.{page})  conf={int}  · {problem}``.
    Each column is padded to the widest value in ``entries`` so the ``·``
    separator (and the problem text after it) line up; the trailing
    ``· {problem}`` segment is omitted when ``problem`` is empty.

    Width scope is the list passed in — callers slice first if they want
    tight alignment for a subset (e.g. terminal top-N) rather than the full
    queue.
    """
    if not entries:
        return []
    prepped = [
        (
            e["student"],
            f"Q{e['question']}",
            f"(p.{e['page']})" if e.get("page") is not None else "(p.?)",
            e["confidence"],
            e.get("problem") or "",
        )
        for e in entries
    ]
    name_w = max(len(t[0]) for t in prepped)
    qnum_w = max(len(t[1]) for t in prepped)
    page_w = max(len(t[2]) for t in prepped)
    conf_w = max(len(str(t[3])) for t in prepped)

    lines: list[str] = []
    for student, q_label, page_str, conf, problem in prepped:
        base = (
            f"{student:<{name_w}}  "
            f"{q_label:<{qnum_w}}  "
            f"{page_str:<{page_w}}  "
            f"conf={conf:>{conf_w}}"
        )
        lines.append(f"{base}  · {problem}" if problem else base)
    return lines


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
            if q.get("_unanswered"):
                continue  # injected row for a skipped scan page — not graded
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

    # Plain-text artifact — one line per entry, columns aligned across the
    # full sorted list (terminal echo computes its own widths over top-N).
    txt_lines = format_review_entry_lines(entries)
    artifact_review_queue_txt_path(artifact_dir).write_text(
        "\n".join(txt_lines) + ("\n" if txt_lines else ""), encoding="utf-8",
    )

    return entries
