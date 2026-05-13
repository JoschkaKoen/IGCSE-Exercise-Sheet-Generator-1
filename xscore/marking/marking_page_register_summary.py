"""Terminal-display summaries for the marking page register.

The register itself (build / augmentation / I/O / iteration) lives in
:mod:`xscore.marking.marking_page_register`. This module owns the
human-readable summary tables emitted by the pipeline:

- :func:`render_cross_page_step_summary` — step 21 (cross-page context)
  detail tables, shown after augmentation runs.
- :func:`print_register_summary` — pre-marking summary table, shown by step
  29 before the AI marking loop begins.

Both are pure presentation code — they never run inside the marking loop —
so they were moved out of ``marking_page_register`` to keep that module
focused on register construction + I/O.
"""

from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Step-21 source-label resolver (used by both summary renderers)
# ---------------------------------------------------------------------------

def _pretty_source_label(
    source: str,
    figure_refs: list[dict],
    parent_refs: list[dict],
    *,
    compact: bool = False,
) -> str | None:
    """Resolve an ``extra_sources`` string to a human-readable display label.

    Returns ``None`` for sources outside the step-21 vocabulary; callers
    filter those out before rendering. The step-21 sources are
    ``"continuation"``, ``"cross_page_fig_*"``, and ``"cross_page_parent_*"``.

    *compact* picks the format for inline use in streaming log lines:
    ``"Q9 p.8"`` / ``"Fig. 1.1 p.2"`` instead of the default
    ``"Q9 (page 8)"`` / ``"Fig. 1.1 (page 2)"``.
    """
    if source == "continuation":
        return "continuation" if compact else "continuation page"
    if source.startswith("cross_page_fig_"):
        label = source[len("cross_page_fig_"):]
        ref = next((r for r in figure_refs if r["figure_label"] == label), None)
        page = ref["drawn_on_answer_label"] if ref else "?"
        return f"Fig. {label} p.{page}" if compact else f"Fig. {label} (page {page})"
    if source.startswith("cross_page_parent_"):
        number = source[len("cross_page_parent_"):]
        ref = next((r for r in parent_refs if r["parent_number"] == number), None)
        page = ref["parent_answer_label"] if ref else "?"
        return f"Q{number} p.{page}" if compact else f"Q{number} (page {page})"
    return None


# ---------------------------------------------------------------------------
# Step-21 (cross-page context) detail tables
# ---------------------------------------------------------------------------

def render_cross_page_step_summary(
    *,
    figure_refs: list[dict],
    parent_refs: list[dict],
    register: dict,
    continuation_refs: list[dict] | None = None,
    console: Any = None,
) -> None:
    """Render step-21 detail tables to the terminal (no-op when no detections).

    Three indented Rich tables, all styled to match :func:`print_register_summary`.
    Per-student data and exam-structure data are split into separate tables
    so the columns mean a single thing per table:

    1. **Continuation pages** — per-student, one row per continuation
       attachment (Student / Scan page / Page type / Attached to).
       Skipped when ``continuation_refs`` is empty.
    2. **Exam-structure references** — figures + parents combined under
       a ``Type`` column (Type / Source / Target). The header line omits
       zero-count types. Skipped when both ``figure_refs`` and
       ``parent_refs`` are empty.
    3. **Calls augmented** — one row per ``(student, call)`` whose call has
       at least one step-21 source. Multiple step-21 sources on the same
       call are joined in the "Extras added" cell.
    """
    continuation_refs = continuation_refs or []
    if not figure_refs and not parent_refs and not continuation_refs:
        return

    from rich import box
    from rich.padding import Padding
    from rich.table import Table

    if console is None:
        from xscore.shared.terminal_ui import get_console
        console = get_console()

    # ── Continuation pages table (per-student) ────────────────────────────
    if continuation_refs:
        cont_table = Table(
            box=box.HORIZONTALS,
            header_style="dim",
            show_edge=False,
            pad_edge=False,
        )
        cont_table.add_column("Student", justify="left")
        cont_table.add_column("Scan page", justify="left")
        cont_table.add_column("Page type", justify="left")
        cont_table.add_column("Attached to", justify="left")
        for r in continuation_refs:
            cont_table.add_row(
                r["student_name"],
                f"p.{r['scan_page']}",
                r["page_type"],
                f"answer p.{r['attached_to_answer_label']}",
            )

        n_cont = len(continuation_refs)
        console.print()
        console.print(
            f"    [dim]Continuation pages — {n_cont} attached[/]"
        )
        console.print(Padding(cont_table, (0, 0, 0, 4), expand=False))

    # ── Exam-structure references table (figures + parents) ───────────────
    struct_rows: list[tuple[str, str, str]] = []
    for r in figure_refs:
        struct_rows.append((
            "figure",
            f"Fig. {r['figure_label']} (page {r['drawn_on_answer_label']})",
            "referenced on page "
            + ", ".join(str(p) for p in r["referenced_on_answer_labels"]),
        ))
    for r in parent_refs:
        children = ", ".join(r["child_numbers"])
        child_pages = sorted(set(r["child_answer_labels"]))
        page_word = "page" if len(child_pages) == 1 else "pages"
        struct_rows.append((
            "parent",
            f"Q{r['parent_number']} (page {r['parent_answer_label']})",
            f"{children} ({page_word} {', '.join(str(p) for p in child_pages)})",
        ))

    if struct_rows:
        struct_table = Table(
            box=box.HORIZONTALS,
            header_style="dim",
            show_edge=False,
            pad_edge=False,
        )
        struct_table.add_column("Type", justify="left")
        struct_table.add_column("Source", justify="left")
        struct_table.add_column("Target", justify="left")
        for row in struct_rows:
            struct_table.add_row(*row)

        n_fig = len(figure_refs)
        n_par = len(parent_refs)
        parts = []
        if n_fig:
            parts.append(f"{n_fig} figure{'s' if n_fig != 1 else ''}")
        if n_par:
            parts.append(f"{n_par} parent{'s' if n_par != 1 else ''}")
        summary = ", ".join(parts)

        console.print()
        console.print(
            f"    [dim]Exam-structure references — {summary}[/]"
        )
        console.print(Padding(struct_table, (0, 0, 0, 4), expand=False))

    # ── Calls augmented table ──────────────────────────────────────────────
    aug_rows: list[tuple[str, str, str]] = []
    students_seen: set[str] = set()
    for student in register.get("students") or []:
        for call in student["calls"]:
            sources = call.get("extra_sources") or []
            labels = [
                lab for s in sources
                if (lab := _pretty_source_label(s, figure_refs, parent_refs)) is not None
            ]
            if not labels:
                continue
            students_seen.add(student["student_name"])
            aug_rows.append((
                student["student_name"],
                f"answer p.{call['answer_label']}",
                ", ".join(f"+ {lab}" for lab in labels),
            ))

    if aug_rows:
        aug_table = Table(
            box=box.HORIZONTALS,
            header_style="dim",
            show_edge=False,
            pad_edge=False,
        )
        aug_table.add_column("Student", justify="left")
        aug_table.add_column("Call", justify="left")
        aug_table.add_column("Extras added", justify="left")
        for row in aug_rows:
            aug_table.add_row(*row)

        console.print()
        console.print(
            f"    [dim]Calls augmented — {len(aug_rows)} call"
            f"{'s' if len(aug_rows) != 1 else ''} across {len(students_seen)} "
            f"student{'s' if len(students_seen) != 1 else ''}[/]"
        )
        console.print(Padding(aug_table, (0, 0, 0, 4), expand=False))
        console.print()


# ---------------------------------------------------------------------------
# Pre-marking terminal display
# ---------------------------------------------------------------------------

def print_register_summary(
    register: dict,
    *,
    cross_page_refs: list[dict] | None = None,
    filtered_call_count: int | None = None,
    filtered_student_count: int | None = None,
    filtered_page_image_count: int | None = None,
    console: Any = None,
) -> None:
    """Render the pre-marking summary table to the terminal.

    Mirrors :func:`xscore.preprocessing.assign_pages_to_students.print_page_range_table`
    visually (Rich Table + dim header, padded title) so the marking step's
    pre-flight display is consistent with the rest of the pipeline.

    *filtered_call_count* / *filtered_student_count*, when provided, indicate
    that the cohort filter narrowed the call list; they're shown alongside
    the unfiltered totals in the header.

    *cross_page_refs* (the diagnostic emitted by step 21) is rendered as a
    short list under the table when non-empty.
    """
    from rich import box
    from rich.padding import Padding
    from rich.table import Table

    if console is None:
        from xscore.shared.terminal_ui import get_console
        console = get_console()

    students = register.get("students") or []
    total_calls = register["metadata"].get("total_calls", sum(len(s["calls"]) for s in students))
    total_students = register["metadata"].get("total_students", len(students))

    if filtered_call_count is not None and filtered_call_count != total_calls:
        call_label = f"{filtered_call_count} calls (filtered from {total_calls})"
    else:
        call_label = f"{total_calls} calls"
    if filtered_student_count is not None and filtered_student_count != total_students:
        student_label = (
            f"{filtered_student_count} student"
            f"{'s' if filtered_student_count != 1 else ''} "
            f"(filtered from {total_students})"
        )
    else:
        student_label = f"{total_students} students"

    table = Table(
        box=box.HORIZONTALS,
        header_style="dim",
        show_edge=False,
        pad_edge=False,
    )
    table.add_column("Student", justify="left")
    table.add_column("Calls", justify="right")
    table.add_column("Pages", justify="right")
    table.add_column("+Cont", justify="right")
    table.add_column("Cross-page", justify="right")

    for s in sorted(students, key=lambda x: _first_primary(x)):
        calls = s["calls"]
        if not calls:
            continue
        primaries = [c["primary_scan_page"] for c in calls]
        page_range = (
            f"{min(primaries)}–{max(primaries)}"
            if min(primaries) != max(primaries)
            else str(primaries[0])
        )
        cont_count = sum(
            1 for c in calls
            if any(src == "continuation" for src in c.get("extra_sources") or [])
        )
        cp_count = sum(
            1 for c in calls
            if any(
                (src or "").startswith("cross_page")
                for src in c.get("extra_sources") or []
            )
        )
        table.add_row(
            s["student_name"],
            str(len(calls)),
            page_range,
            str(cont_count),
            str(cp_count),
        )

    console.print()
    console.print(
        f"    [dim]Marking page register — {call_label} across {student_label}[/]"
    )
    console.print(Padding(table, (0, 0, 0, 4), expand=False))
    console.print()

    # ── Excluded pages sub-table ─────────────────────────────────────────────
    excluded_rows = []
    for s in sorted(students, key=lambda x: _first_primary(x)):
        cover = s.get("cover_page_number")
        skipped = s.get("skipped_scan_pages") or []
        if cover is None and not skipped:
            continue
        excluded_rows.append((s["student_name"], cover, skipped))

    if excluded_rows:
        excluded_table = Table(
            box=box.HORIZONTALS,
            header_style="dim",
            show_edge=False,
            pad_edge=False,
        )
        excluded_table.add_column("Student", justify="left")
        excluded_table.add_column("Cover", justify="right")
        excluded_table.add_column("No handwriting", justify="left")
        for name, cover, skipped in excluded_rows:
            excluded_table.add_row(
                name,
                str(cover) if cover is not None else "—",
                _format_skipped_pages(skipped),
            )
        console.print("    [dim]Excluded pages per student[/]")
        console.print(Padding(excluded_table, (0, 0, 0, 4), expand=False))
        console.print()

    if cross_page_refs:
        console.print(f"    [dim]Cross-page figures detected ({len(cross_page_refs)}):[/]")
        for ref in cross_page_refs:
            referenced = ", ".join(str(p) for p in ref["referenced_on_answer_labels"])
            console.print(
                f"      [dim]Fig. {ref['figure_label']} — drawn on answer page "
                f"{ref['drawn_on_answer_label']}, also referenced on page"
                f"{'s' if len(ref['referenced_on_answer_labels']) != 1 else ''} {referenced}[/]"
            )

    # Totals: page-images = sum of len(scan_pages) across surviving calls.
    # Use the filtered count when provided so the printed equation
    # ``{active_calls} × {avg} = {page_image_count}`` actually balances.
    if filtered_call_count is not None and filtered_call_count != total_calls:
        active_calls = filtered_call_count
        if filtered_page_image_count is not None:
            page_image_count = filtered_page_image_count
        else:
            page_image_count = sum(
                len(c["scan_pages"]) for s in students for c in s["calls"]
            )
    else:
        active_calls = total_calls
        page_image_count = sum(
            len(c["scan_pages"]) for s in students for c in s["calls"]
        )
    if active_calls:
        avg = page_image_count / max(active_calls, 1)
        console.print(
            f"    [dim]Marking {page_image_count} pages in {active_calls} "
            f"calls (avg {avg:.2f} pages/call)[/]"
        )


# ---------------------------------------------------------------------------
# Small helpers used by the summary renderers
# ---------------------------------------------------------------------------

def _first_primary(student: dict) -> int:
    """Sort key: first primary scan page (matches step 15's print_page_range_table)."""
    calls = student.get("calls") or []
    if not calls:
        return 1 << 30   # sort empties to the end
    return calls[0]["primary_scan_page"]


def _format_skipped_pages(pages: list[int], *, max_ranges: int = 4) -> str:
    """Group contiguous integers into en-dash ranges, with overflow truncation.

    [5,6,7,8,9,12,14,15,16] → "5–9, 12, 14–16"
    Truncates to *max_ranges* ranges and appends "(… N more)" for the rest.
    Empty list → "—".
    """
    if not pages:
        return "—"
    sorted_pages = sorted(set(int(p) for p in pages))
    ranges: list[tuple[int, int]] = []
    start = end = sorted_pages[0]
    for p in sorted_pages[1:]:
        if p == end + 1:
            end = p
        else:
            ranges.append((start, end))
            start = end = p
    ranges.append((start, end))

    parts = [f"{s}–{e}" if s != e else str(s) for s, e in ranges]
    if len(ranges) <= max_ranges:
        return ", ".join(parts)
    kept = parts[:max_ranges]
    rest_count = sum(e - s + 1 for s, e in ranges[max_ranges:])
    return ", ".join(kept) + f" (… {rest_count} more)"
