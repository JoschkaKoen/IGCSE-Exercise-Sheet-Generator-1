"""Review queue: human-friendly per-question confidence audit.

Two exports consumed by other modules:

- :func:`format_review_entry_lines` — terminal-friendly row formatter,
  used by ``steps/reports.py`` for the run-end summary.
- :func:`_write_review_queue` — JSON + Markdown + text artifact writer,
  used by ``merge_reports.py`` after every per-student YAML lands.

Extracted from ``class_report_export`` so the XLSX writer can live in its
own module without dragging the review-queue concerns along.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from xscore.shared.exam_paths import (
    artifact_review_queue_json_path,
    artifact_review_queue_md_path,
    artifact_review_queue_txt_path,
)
from xscore.shared.terminal_ui import warn_line


def _qnum_natural_key(qnum: str) -> tuple:
    """Sort '2' before '10', and 'Q_2' suffix after its base."""
    base, _, suffix = qnum.partition("_")
    try:
        return (0, int(base), suffix)
    except ValueError:
        return (1, qnum, "")


_ANS_MAX = 16  # max chars per side of the A→B pair before truncation


def _short_answer(text: Any) -> str:
    """Flatten whitespace and truncate so multi-line LaTeX/code answers
    don't blow up the per-row column width. Empty → ``"?"``."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return "?"
    return s if len(s) <= _ANS_MAX else s[: _ANS_MAX - 1].rstrip() + "…"


def _marks_cell(am: Any, mm: Any) -> str:
    a = "?" if am is None else str(am)
    m = "?" if mm is None else str(mm)
    return f"{a}/{m}"


def format_review_entry_lines(entries: list[dict]) -> list[str]:
    """Format queue entries as one aligned line each, for terminal echo /
    ``review.txt``.

    Layout per row:
    ``{student}  Q{qnum}  (p.{page})  conf={int}  {sa→ca}  {am/mm}  · {problem}``.
    Each column is padded to the widest value in ``entries`` so the ``·``
    separator (and the problem text after it) line up; the trailing
    ``· {problem}`` segment is omitted when ``problem`` is empty. Multi-line
    or long ``student_answer`` / ``correct_answer`` strings are flattened
    and truncated by :func:`_short_answer` so a single big answer can't
    widen every row.

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
            f"{_short_answer(e.get('student_answer'))}→"
            f"{_short_answer(e.get('correct_answer'))}",
            _marks_cell(e.get("assigned_marks"), e.get("max_marks")),
            e.get("problem") or "",
        )
        for e in entries
    ]
    name_w  = max(len(t[0]) for t in prepped)
    qnum_w  = max(len(t[1]) for t in prepped)
    page_w  = max(len(t[2]) for t in prepped)
    conf_w  = max(len(str(t[3])) for t in prepped)
    ans_w   = max(len(t[4]) for t in prepped)
    marks_w = max(len(t[5]) for t in prepped)

    lines: list[str] = []
    for student, q_label, page_str, conf, ans_pair, marks, problem in prepped:
        base = (
            f"{student:<{name_w}}  "
            f"{q_label:<{qnum_w}}  "
            f"{page_str:<{page_w}}  "
            f"conf={conf:>{conf_w}}  "
            f"{ans_pair:<{ans_w}}  "
            f"{marks:<{marks_w}}"
        )
        lines.append(f"{base}  · {problem}" if problem else base.rstrip())
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
