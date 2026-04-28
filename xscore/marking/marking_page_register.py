"""Marking page register — explicit, persisted record of every AI marking call.

The register answers the question "for student S, marking answer-page P, which
scan pages will the AI see in one call?" Today this decision is computed
inline at marking time in :func:`xscore.marking.ai_mark.run_ai_marking`; this
module hoists that logic into a persisted artifact so it can be inspected,
diffed, and refined by additional steps (e.g. cross-page figure detection in
step 19) before any expensive marking work happens.

Lifecycle:

1. **Step 15** (handwriting check) writes the *initial* register
   (``15_student_handwriting/marking_page_register.json``) with one primary
   scan page per call plus extras coming from blank-page-with-handwriting
   attachments.
2. **Step 19** (cross-page figure detection) reads register v1, augments calls
   whose answer pages reference figures drawn elsewhere in the exam, and
   writes ``19_detect_cross_page_figures/marking_page_register.json``.
3. **Step 25** (AI marking) loads the most-refined register available and
   iterates the calls. Two filters that *cannot* be baked in (scaffold-bounds
   cap and CLI cohort filter) are applied at iteration time.

Schema is documented in the project plan; see also :func:`build_initial_register`
for the source of truth.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xscore.shared.path_builders import (
    artifact_marking_page_register_v1_path,
    artifact_marking_page_register_v2_path,
)

if TYPE_CHECKING:
    from xscore.shared.pipeline_ctx import _Ctx


SCHEMA_VERSION = 1
REGISTER_FILENAME = "marking_page_register.json"


# ---------------------------------------------------------------------------
# Builder — used by step 15 (writes v1) and the step 25 backwards-compat path
# ---------------------------------------------------------------------------

def build_initial_register(ctx: "_Ctx") -> dict:
    """Build register v1 from page assignments + handwriting check.

    Pure function: reads the on-disk artifacts of steps 12 and 15 plus
    ``ctx.empty_exam_has_cover``. Returns the in-memory register dict.

    Reproduces the loop from the legacy bundling logic in
    :func:`xscore.marking.ai_mark.run_ai_marking` (cover-skip, handwriting
    skip, handwriting-extras attach), but does NOT apply the scaffold-bounds
    cap or any cohort filter — those are runtime concerns of step 25.
    """
    from xscore.shared.exam_paths import (
        artifact_exam_student_list_json_path,
        artifact_handwriting_json_path,
    )

    assert ctx.artifact_dir is not None
    list_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
    raw_assignments: list[dict] = json.loads(list_path.read_text(encoding="utf-8"))

    skip_by_student, extras_by_student = _load_handwriting(
        artifact_handwriting_json_path(ctx.artifact_dir)
    )

    empty_exam_has_cover = bool(ctx.empty_exam_has_cover)
    students_out: list[dict] = []
    total_calls = 0
    for a in raw_assignments:
        student_name = a["student_name"]
        page_numbers: list[int] = a["page_numbers"]
        cover_page_number = a.get("cover_page_number")
        has_cover = cover_page_number is not None
        cover_offset = 1 if (has_cover and not empty_exam_has_cover) else 0
        student_skip = skip_by_student.get(student_name, set())
        student_extras = extras_by_student.get(student_name, {})

        calls: list[dict] = []
        for p_label, scan_page in enumerate(page_numbers, 1):
            if has_cover and p_label == 1:
                continue   # cover page never marked
            answer_label = p_label - cover_offset
            if scan_page in student_skip:
                continue   # blank page with no handwriting
            extras = list(student_extras.get(scan_page, []))
            sources = ["handwriting"] * len(extras)
            calls.append({
                "p_label": p_label,
                "answer_label": answer_label,
                "primary_scan_page": scan_page,
                "extra_scan_pages": extras,
                "extra_sources": sources,
                "scan_pages": [scan_page] + extras,
            })

        students_out.append({
            "student_name": student_name,
            "cover_page_number": cover_page_number,
            "page_numbers": list(page_numbers),
            "answer_page_count": len(page_numbers) - (1 if has_cover else 0),
            "skipped_scan_pages": sorted(student_skip),
            "calls": calls,
        })
        total_calls += len(calls)

    return {
        "metadata": {
            "schema_version": SCHEMA_VERSION,
            "produced_by_step": 15,
            "produced_by_step_name": "student_handwriting_check",
            "empty_exam_has_cover": empty_exam_has_cover,
            "applied_extras": ["handwriting"],
            "total_students": len(students_out),
            "total_calls": total_calls,
        },
        "students": students_out,
    }


def _load_handwriting(
    handwriting_path: Path,
) -> tuple[dict[str, set[int]], dict[str, dict[int, list[int]]]]:
    """Parse handwriting.json into (skip_set, extras_map) keyed by student.

    ``skip_set`` lists scan pages with no handwriting (the AI should never
    see them); ``extras_map`` says "for student S, when marking scan-page P,
    also include these blank-but-handwritten extras".

    Prefers the per-student ``pages_without_handwriting`` field as the
    authoritative skip list (written by step 15 under
    ``HANDWRITING_CHECK_WIDE=1``). Falls back to deriving the skip set from
    ``blank_scan_pages`` entries with ``has_handwriting=false`` for
    handwriting.json files written before that field existed.

    The extras map is always derived from ``blank_scan_pages`` —
    attach-to-previous semantics only apply to blank-in-empty pages.
    """
    skip_by_student: dict[str, set[int]] = {}
    extras_by_student: dict[str, dict[int, list[int]]] = {}
    if not handwriting_path.exists():
        return skip_by_student, extras_by_student

    data = json.loads(handwriting_path.read_text(encoding="utf-8"))
    for s in data.get("students", []):
        student_name = s.get("student_name")
        if student_name is None:
            continue

        new_field = s.get("pages_without_handwriting")
        if new_field is not None:
            skip: set[int] = {int(p) for p in new_field}
        else:
            skip = {
                bp["scan_page"]
                for bp in s.get("blank_scan_pages", [])
                if bp.get("scan_page") is not None
                and not bp.get("has_handwriting", False)
            }

        extras: dict[int, list[int]] = {}
        for bp in s.get("blank_scan_pages", []):
            scan_page = bp.get("scan_page")
            if scan_page is None:
                continue
            if bp.get("has_handwriting", False) and bp.get("attach_to_scan_page") is not None:
                extras.setdefault(bp["attach_to_scan_page"], []).append(scan_page)

        skip_by_student[student_name] = skip
        extras_by_student[student_name] = extras
    return skip_by_student, extras_by_student


# ---------------------------------------------------------------------------
# Cross-page figure-reference detection (step 19)
# ---------------------------------------------------------------------------

# Matches "Fig. 1.1", "Fig 1.1", "Figure 1.1", "Fig. 5" — case-insensitive.
_FIGURE_RE = re.compile(r"(?i)\bfig(?:ure|\.)?\s*(\d+(?:\.\d+)?)")


def apply_cross_page_extras(
    register: dict,
    exam_questions_yaml: dict,
    empty_exam_has_cover: bool,
) -> tuple[dict, list[dict]]:
    """Augment *register* (in-place copy) with cross-page figure extras.

    Returns ``(updated_register, cross_page_refs)`` where ``cross_page_refs``
    is a flat list of ``{figure_label, drawn_on_*, referenced_on_*}`` dicts
    suitable for writing as a diagnostic.

    Figure → page resolution uses the **first textual mention** heuristic:
    the figure lives on the smallest-numbered page that mentions it. If the
    figure is mentioned on multiple pages, every page other than the first is
    a "cross-page reference" and gets the first-mention page added as an
    extra scan page in the register.
    """
    questions = exam_questions_yaml.get("questions") or []
    mentions = list(_walk_figure_mentions(questions))

    figure_first_page: dict[str, int] = {}
    for fig_label, exam_page in mentions:
        prev = figure_first_page.get(fig_label)
        if prev is None or exam_page < prev:
            figure_first_page[fig_label] = exam_page

    # extras_by_answer_label: {answer_label_referencing → set(answer_label_drawn)}
    extras_by_answer_label: dict[int, set[int]] = {}
    cross_page_refs_by_label: dict[str, dict] = {}
    for fig_label, ref_page in mentions:
        drawn_page = figure_first_page[fig_label]
        if drawn_page == ref_page:
            continue
        ref_answer = _exam_page_to_answer_label(ref_page, empty_exam_has_cover)
        drawn_answer = _exam_page_to_answer_label(drawn_page, empty_exam_has_cover)
        if ref_answer is None or drawn_answer is None:
            continue
        extras_by_answer_label.setdefault(ref_answer, set()).add(drawn_answer)
        entry = cross_page_refs_by_label.setdefault(fig_label, {
            "figure_label": fig_label,
            "drawn_on_empty_exam_page": drawn_page,
            "drawn_on_answer_label": drawn_answer,
            "referenced_on_empty_exam_pages": [],
            "referenced_on_answer_labels": [],
        })
        if ref_page not in entry["referenced_on_empty_exam_pages"]:
            entry["referenced_on_empty_exam_pages"].append(ref_page)
            entry["referenced_on_answer_labels"].append(ref_answer)

    cross_page_refs = sorted(
        cross_page_refs_by_label.values(),
        key=lambda r: (r["drawn_on_empty_exam_page"], r["figure_label"]),
    )
    for entry in cross_page_refs:
        entry["referenced_on_empty_exam_pages"].sort()
        entry["referenced_on_answer_labels"].sort()

    # Apply extras to each student's calls.
    for student in register["students"]:
        cover_page_number = student.get("cover_page_number")
        page_numbers = student.get("page_numbers") or []
        student_cover_offset = (
            1 if (cover_page_number is not None and not empty_exam_has_cover) else 0
        )
        # Pages the student left blank — never useful as a cross-page figure
        # source even if the empty-exam template has the figure drawn there.
        skipped = set(student.get("skipped_scan_pages") or [])
        for call in student["calls"]:
            x = call["answer_label"]
            extras_for_x = extras_by_answer_label.get(x, set())
            if not extras_for_x:
                continue
            existing_extras = list(call["extra_scan_pages"])
            existing_sources = list(call["extra_sources"])
            for y in sorted(extras_for_x):
                p_label_y = y + student_cover_offset
                if not (1 <= p_label_y <= len(page_numbers)):
                    continue
                scan_page_y = page_numbers[p_label_y - 1]
                if scan_page_y == call["primary_scan_page"]:
                    continue
                if scan_page_y in existing_extras:
                    continue
                if scan_page_y in skipped:
                    continue   # student left this page blank — no figure to send
                # Find the figure label(s) for the diagnostic source string.
                figs_for_y = sorted(
                    label for label, pg in figure_first_page.items()
                    if _exam_page_to_answer_label(pg, empty_exam_has_cover) == y
                )
                source = (
                    f"cross_page_fig_{figs_for_y[0]}" if figs_for_y else "cross_page"
                )
                existing_extras.append(scan_page_y)
                existing_sources.append(source)
            call["extra_scan_pages"] = existing_extras
            call["extra_sources"] = existing_sources
            call["scan_pages"] = [call["primary_scan_page"]] + existing_extras

    # Refresh metadata.
    md = register["metadata"]
    md["produced_by_step"] = 19
    md["produced_by_step_name"] = "detect_cross_page_figures"
    if "cross_page_figures" not in md.get("applied_extras", []):
        md["applied_extras"] = list(md.get("applied_extras", [])) + ["cross_page_figures"]
    md["total_calls"] = sum(len(s["calls"]) for s in register["students"])

    return register, cross_page_refs


def _walk_figure_mentions(questions: list[dict]) -> Iterator[tuple[str, int]]:
    """Yield (figure_label, empty_exam_page) for every figure mention in *questions*.

    Recurses into ``subquestions``. The same (figure_label, page) pair can be
    yielded multiple times — that's fine; consumers normalise.
    """
    for q in questions:
        page = q.get("page")
        text = q.get("text") or ""
        if isinstance(page, int) and text:
            for m in _FIGURE_RE.finditer(text):
                yield m.group(1), page
        for sub in q.get("subquestions") or []:
            yield from _walk_figure_mentions([sub])


def _exam_page_to_answer_label(empty_exam_page: int, empty_exam_has_cover: bool) -> int | None:
    """Convert a 1-based empty-exam page to the scaffold answer_label.

    The pipeline uses ``answer_label = empty_exam_page`` directly — the cover
    page is filtered out at marking time via the ``p_label == 1`` skip in
    :func:`build_initial_register`, not by answer_label arithmetic. So this
    function just rejects the cover page (page 1 when the empty exam has one)
    and passes everything else through.
    """
    if empty_exam_has_cover and empty_exam_page == 1:
        return None
    return empty_exam_page


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------

def write_register(path: Path, register: dict) -> None:
    """Serialise *register* as pretty-printed JSON, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(register, indent=2, ensure_ascii=False), encoding="utf-8")


def load_register(artifact_dir: Path) -> dict | None:
    """Return the most-refined register available, or ``None`` if none exists.

    Tries the step 19 register first (cross-page-figure-augmented), falling
    back to the step 15 register (handwriting-extras only). Returns ``None``
    when neither file exists — callers handle that via an in-memory rebuild
    (e.g. when resuming an old run from before this artifact existed).
    """
    candidates = (
        artifact_marking_page_register_v2_path(artifact_dir),
        artifact_marking_page_register_v1_path(artifact_dir),
    )
    for candidate in candidates:
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
    return None


# ---------------------------------------------------------------------------
# Iteration with runtime filters (consumed by step 25)
# ---------------------------------------------------------------------------

def iter_marking_calls(
    register: dict,
    *,
    raw_assignments: list[dict],
    scaffold_page_count: int | None = None,
    student_filter: set[str] | None = None,
) -> Iterator[tuple[dict, int, int, int, list[int]]]:
    """Yield ``(assignment, p_label, answer_label, answer_page_count, extra_scan_pages)``.

    Mirrors the tuple shape that ``run_ai_marking`` consumed pre-refactor so
    the call-site change in :mod:`xscore.marking.ai_mark` is minimal.

    *raw_assignments* is the parsed ``exam_student_list.json`` content; it's
    needed because the marking loop uses the original ``page_numbers`` list
    for rendering and labelling. The register knows which calls to make; the
    raw list provides the per-student pixel-level metadata.

    Filters applied:
    - **scaffold-bounds cap**: drops calls with
      ``answer_label > scaffold_page_count`` (only when *scaffold_page_count*
      is provided).
    - **cohort filter**: drops students whose ``student_name`` is not in
      *student_filter* (only when the set is provided; ``None`` = include all).
    """
    by_name = {a["student_name"]: a for a in raw_assignments}
    for student in register["students"]:
        name = student["student_name"]
        if student_filter is not None and name not in student_filter:
            continue
        assignment = by_name.get(name)
        if assignment is None:
            continue   # register references a student no longer in the roster
        answer_page_count = student["answer_page_count"]
        for call in student["calls"]:
            answer_label = call["answer_label"]
            if scaffold_page_count is not None and answer_label > scaffold_page_count:
                continue
            yield (
                assignment,
                call["p_label"],
                answer_label,
                answer_page_count,
                list(call["extra_scan_pages"]),
            )


# ---------------------------------------------------------------------------
# Pre-marking terminal display
# ---------------------------------------------------------------------------

def print_register_summary(
    register: dict,
    *,
    cross_page_refs: list[dict] | None = None,
    filtered_call_count: int | None = None,
    filtered_student_count: int | None = None,
    console: Any = None,
) -> None:
    """Render the pre-marking summary table to the terminal.

    Mirrors :func:`xscore.preprocessing.assign_pages_to_students.print_page_range_table`
    visually (Rich Table + dim header, padded title) so the marking step's
    pre-flight display is consistent with the rest of the pipeline.

    *filtered_call_count* / *filtered_student_count*, when provided, indicate
    that the cohort filter narrowed the call list; they're shown alongside
    the unfiltered totals in the header.

    *cross_page_refs* (the diagnostic emitted by step 19) is rendered as a
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
    table.add_column("Handwriting", justify="right")
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
        hw_count = sum(
            1 for c in calls
            if any(src == "handwriting" for src in c.get("extra_sources") or [])
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
            str(hw_count),
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
    page_image_count = sum(
        len(c["scan_pages"]) for s in students for c in s["calls"]
    )
    if filtered_call_count is not None and filtered_call_count != total_calls:
        active_calls = filtered_call_count
    else:
        active_calls = total_calls
    if active_calls:
        avg = page_image_count / max(total_calls, 1)
        console.print(
            f"    [dim]{active_calls} calls × {avg:.2f} avg pages/call = "
            f"{page_image_count} page-images about to be sent to the marking AI.[/]"
        )


def _first_primary(student: dict) -> int:
    """Sort key: first primary scan page (matches step 12's print_page_range_table)."""
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
