"""Display-ordering helpers for step 28 (extract_student_answers).

Step 28 runs per-(student, page) extraction in a thread pool and prints a
progress line for every page in the scan PDF — extracted, cover-skipped, and
no-handwriting-skipped alike. Workers complete in arbitrary order, but the
output is grouped by student name and ascending page number via a streaming
reorder buffer.

These helpers are factored out of ``extract_answers.py`` to keep that module
focused on the extraction logic itself.
"""

from __future__ import annotations

import threading
from typing import Any, Callable


def build_display_entries(
    register: dict, raw_assignments: list[dict]
) -> tuple[list[dict], dict[tuple[str, int], int], int, int, int, int]:
    """Enumerate every (student, p_label) pair in the (filtered) raw_assignments
    set with its status — ``extract``, ``cover``, or ``no_handwriting`` — sorted
    by (student_name, p_label), with a ``banner`` row inserted before each
    student's first entry.

    Returns ``(entries, idx_by_key, total_pdf_pages, n_cover, n_no_handwriting,
    n_students)``. ``total_pdf_pages`` is computed from the *full* register
    (every student), so the "/N" denominator is stable when the caller filters
    to a subset. ``idx_by_key`` only keys non-banner rows; banner rows occupy
    their own slots in the reorder buffer and are addressed by enumeration.
    """
    total_pdf_pages = sum(len(s["page_numbers"]) for s in register["students"])
    register_by_name = {s["student_name"]: s for s in register["students"]}
    per_student: dict[str, list[dict]] = {}
    students_in_order: list[str] = []
    n_cover = 0
    n_no_hw = 0
    for a in raw_assignments:
        s = register_by_name.get(a["student_name"])
        if s is None:
            continue
        if s["student_name"] not in per_student:
            per_student[s["student_name"]] = []
            students_in_order.append(s["student_name"])
        has_cover = s["cover_page_number"] is not None
        student_skip = set(s["skipped_scan_pages"])
        student_total = len(s["page_numbers"])
        for p_label, scan_page in enumerate(s["page_numbers"], 1):
            if has_cover and p_label == 1:
                status = "cover"
                n_cover += 1
            elif scan_page in student_skip:
                status = "no_handwriting"
                n_no_hw += 1
            else:
                status = "extract"
            per_student[s["student_name"]].append({
                "student_name": s["student_name"], "p_label": p_label,
                "scan_page": scan_page, "student_total": student_total,
                "status": status,
            })

    entries: list[dict] = []
    for i, name in enumerate(students_in_order):
        rows = sorted(per_student[name], key=lambda e: e["p_label"])
        n_extract = sum(1 for r in rows if r["status"] == "extract")
        student_total = rows[0]["student_total"] if rows else 0
        entries.append({
            "student_name": name, "status": "banner",
            "n_extract": n_extract, "student_total": student_total,
            "is_first": i == 0,
        })
        entries.extend(rows)

    idx_by_key = {
        (e["student_name"], e["p_label"]): i
        for i, e in enumerate(entries)
        if e["status"] != "banner"
    }
    return entries, idx_by_key, total_pdf_pages, n_cover, n_no_hw, len(students_in_order)


def make_reorder_buffer(console: Any) -> Callable[[int, str], None]:
    """Return a thread-safe ``emit_ordered(idx, line)`` backed by a streaming
    reorder buffer.

    Lines submitted with non-monotonic ``idx`` are held until the next-expected
    ``idx`` arrives, then drained in order. Every ``idx`` from 0..N-1 must be
    submitted exactly once or the buffer stalls.
    """
    buffer: dict[int, str] = {}
    next_idx: list[int] = [0]
    lock = threading.Lock()

    def emit_ordered(idx: int, line: str) -> None:
        with lock:
            buffer[idx] = line
            while next_idx[0] in buffer:
                console.print(buffer.pop(next_idx[0]))
                next_idx[0] += 1

    return emit_ordered


def emit_banner_lines(
    display_entries: list[dict],
    emit_ordered: Callable[[int, str], None],
    icon_fn: Callable[[str], str],
) -> None:
    """Pre-seed the reorder buffer with one banner per student.

    Each banner introduces a student's block of per-page lines. A leading
    newline is prepended to every banner except the first, so a blank line
    falls between student blocks when the buffer drains in order.
    """
    for i, e in enumerate(display_entries):
        if e["status"] != "banner":
            continue
        prefix = "" if e["is_first"] else "\n"
        line = (
            f"{prefix}[bold]  {icon_fn('info')}  {e['student_name']}"
            f"  —  {e['n_extract']} of {e['student_total']} pages[/]"
        )
        emit_ordered(i, line)


def emit_skipped_lines(
    display_entries: list[dict],
    idx_by_key: dict[tuple[str, int], int],
    total_pdf_pages: int,
    emit_ordered: Callable[[int, str], None],
    icon_fn: Callable[[str], str],
) -> None:
    """Pre-seed the reorder buffer with cover and no-handwriting lines.

    Each pre-seeded line carries its absolute display ``idx``; the buffer then
    holds them until the parallel extraction workers fill the gaps.
    """
    for e in display_entries:
        if e["status"] not in ("cover", "no_handwriting"):
            continue
        idx = idx_by_key[(e["student_name"], e["p_label"])]
        if e["status"] == "cover":
            line = (
                f"[dim]     {icon_fn('info')}  page {e['scan_page']:>3}/{total_pdf_pages}"
                f"  ·  ans p {e['p_label']:>2}/{e['student_total']}"
                f"  ·  cover (skipped)[/]"
            )
        else:  # no_handwriting
            line = (
                f"[dim]     {icon_fn('info')}  page {e['scan_page']:>3}/{total_pdf_pages}"
                f"  ·  ans p {e['p_label']:>2}/{e['student_total']}"
                f"  ·  blank (skipped)[/]"
            )
        emit_ordered(idx, line)
