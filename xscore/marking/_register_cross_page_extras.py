"""Step-21 augmentations to the marking page register.

Three augmentation passes (continuation pages, cross-page figures, parent
question stems) plus the shared attachment-application helper and the
``_exam_page_to_answer_label`` mapper. Public entry:
:func:`apply_cross_page_extras`.

Extracted from ``marking_page_register`` as the second-pass split — the
main module keeps step-18 register construction + I/O + iteration.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def apply_cross_page_extras(
    register: dict,
    exam_questions_yaml: dict,
    empty_exam_has_cover: bool,
    *,
    empty_classifications: list[dict],
    detect_parents: bool = True,
) -> tuple[dict, list[dict], list[dict], list[dict]]:
    """Augment *register* (in-place) with cross-page context extras.

    Three passes:

    1. **Continuation pass** — for each call whose ``answer_label`` matches
       an empty-exam page classified as ``blank page`` or ``writing space
       page``, remove the primary call and attach the scan page to the most
       recent preceding ``question page`` call as an extra with
       ``source="continuation"``. Runs FIRST so later passes don't waste
       work attaching to calls that are about to be removed.
    2. **Figure pass** — for each "Fig. N.N" mention found on a page other
       than the figure's first-mention (drawn-on) page, attach the drawn-on
       page as an extra. Respects the student's ``skipped_scan_pages``: a
       page the student left blank carries no useful figure pixels.
    3. **Parent pass** — for each child question whose page is later than an
       ancestor's, attach the ancestor's page as an extra. *Bypasses* the
       ``skipped_scan_pages`` check because parent pages typically have no
       student handwriting (printed flowchart / stem only) — that's the
       very content we want the marker to see. Skipped when
       ``detect_parents=False`` (controlled by the
       ``CROSS_PAGE_PARENT_DETECTION`` env toggle at the step boundary).

    Returns ``(register, figure_refs, parent_refs, continuation_refs)``
    where each ``*_refs`` is a flat list of diagnostic dicts suitable for
    writing next to the register.
    """
    questions = exam_questions_yaml.get("questions") or []

    # ── Pass 1: continuation pages ──────────────────────────────────────────
    continuation_refs = _apply_continuation_extras(register, empty_classifications)

    # ── Pass 2: figure mentions ─────────────────────────────────────────────
    figure_attachments, figure_refs = _compute_figure_attachments(
        questions, empty_exam_has_cover,
    )
    _apply_attachments(
        register, figure_attachments, empty_exam_has_cover,
        bypass_skipped=False,
    )

    # ── Pass 3: parent stems ────────────────────────────────────────────────
    if detect_parents:
        parent_attachments, parent_refs = _compute_parent_attachments(
            questions, empty_exam_has_cover,
        )
        _apply_attachments(
            register, parent_attachments, empty_exam_has_cover,
            bypass_skipped=True,
        )
    else:
        parent_refs = []

    # ── Refresh metadata ───────────────────────────────────────────────────
    md = register["metadata"]
    md["produced_by_step"] = 21
    md["produced_by_step_name"] = "detect_cross_page_context"
    applied = list(md.get("applied_extras", []))
    if continuation_refs and "continuation" not in applied:
        applied.append("continuation")
    if figure_refs and "cross_page_figures" not in applied:
        applied.append("cross_page_figures")
    if parent_refs and "cross_page_parents" not in applied:
        applied.append("cross_page_parents")
    md["applied_extras"] = applied
    md["total_calls"] = sum(len(s["calls"]) for s in register["students"])

    return register, figure_refs, parent_refs, continuation_refs


# ---------------------------------------------------------------------------
# Pass 1: continuation pages (blank + writing-space pages with handwriting)
# ---------------------------------------------------------------------------

_CONTINUATION_PAGE_TYPES = frozenset({"blank page", "writing space page"})


def _apply_continuation_extras(
    register: dict,
    empty_classifications: list[dict],
) -> list[dict]:
    """Remove primary calls for blank/writing-space pages and attach as extras.

    For each call whose ``answer_label`` matches an empty-exam page
    classified as ``blank page`` or ``writing space page``: remove the
    primary call and attach its scan page to the most recent preceding
    ``question page`` call as an extra with ``source="continuation"``.

    A run of consecutive overflow pages all attach to the same preceding
    question page in scan-page order, so the AI marker sees the question
    page first followed by all overflow pages top-to-bottom.

    Inputs come from step 14's classifications and the register itself —
    has_handwriting filtering is already done by step 18 (every call left
    in the register has handwriting), so this pass does not need
    handwriting.json.

    Edge cases (all skip silently, matching prior behavior):
    - Orphan overflow at the start of an exam (no preceding question page).
    - The preceding question page itself was filtered out (no handwriting).
    """
    if not empty_classifications:
        return []
    page_type_by_page: dict[int, str] = {
        c["page"]: c["page_type"]
        for c in empty_classifications
        if c.get("page") is not None and c.get("page_type") is not None
    }
    content_pages = sorted(
        p for p, pt in page_type_by_page.items() if pt == "question page"
    )
    if not content_pages:
        return []

    continuation_refs: list[dict] = []
    for student in register["students"]:
        # Iterate over a copy — we mutate student["calls"] inside the loop.
        for call in list(student["calls"]):
            answer_label = call["answer_label"]
            page_type = page_type_by_page.get(answer_label)
            if page_type not in _CONTINUATION_PAGE_TYPES:
                continue
            primary_scan = call["primary_scan_page"]
            student["calls"].remove(call)
            skipped = list(student.get("skipped_scan_pages") or [])
            if primary_scan not in skipped:
                skipped.append(primary_scan)
                skipped.sort()
                student["skipped_scan_pages"] = skipped
            # Most recent preceding question page in empty-exam order.
            attach_answer = next(
                (p for p in reversed(content_pages) if p < answer_label),
                None,
            )
            if attach_answer is None:
                continue
            attach_call = next(
                (c for c in student["calls"] if c["answer_label"] == attach_answer),
                None,
            )
            if attach_call is None:
                continue
            if primary_scan in attach_call["extra_scan_pages"]:
                continue
            attach_call["extra_scan_pages"].append(primary_scan)
            attach_call["extra_sources"].append("continuation")
            attach_call["scan_pages"] = (
                [attach_call["primary_scan_page"]] + attach_call["extra_scan_pages"]
            )
            continuation_refs.append({
                "student_name": student["student_name"],
                "scan_page": primary_scan,
                "answer_label": answer_label,
                "page_type": page_type,
                "attached_to_answer_label": attach_answer,
                "attached_to_scan_page": attach_call["primary_scan_page"],
            })
    return continuation_refs


# ---------------------------------------------------------------------------
# Pass 1: figure mentions
# ---------------------------------------------------------------------------

def _compute_figure_attachments(
    questions: list[dict],
    empty_exam_has_cover: bool,
) -> tuple[_AttachMap, list[dict]]:
    """Resolve "Fig. N.N" mentions into (attachments, diagnostic refs).

    Figure → page resolution uses the first-textual-mention heuristic: the
    figure lives on the smallest-numbered page that mentions it.
    """
    mentions = list(_walk_figure_mentions(questions))

    figure_first_page: dict[str, int] = {}
    for fig_label, exam_page in mentions:
        prev = figure_first_page.get(fig_label)
        if prev is None or exam_page < prev:
            figure_first_page[fig_label] = exam_page

    attachments: _AttachMap = {}
    refs_by_label: dict[str, dict] = {}
    seen_pairs: set[tuple[int, int, str]] = set()
    for fig_label, ref_page in mentions:
        drawn_page = figure_first_page[fig_label]
        if drawn_page == ref_page:
            continue
        ref_answer = _exam_page_to_answer_label(ref_page, empty_exam_has_cover)
        drawn_answer = _exam_page_to_answer_label(drawn_page, empty_exam_has_cover)
        if ref_answer is None or drawn_answer is None:
            continue
        source = f"cross_page_fig_{fig_label}"
        key = (ref_answer, drawn_answer, source)
        if key not in seen_pairs:
            attachments.setdefault(ref_answer, []).append((drawn_answer, source))
            seen_pairs.add(key)
        entry = refs_by_label.setdefault(fig_label, {
            "figure_label": fig_label,
            "drawn_on_empty_exam_page": drawn_page,
            "drawn_on_answer_label": drawn_answer,
            "referenced_on_empty_exam_pages": [],
            "referenced_on_answer_labels": [],
        })
        if ref_page not in entry["referenced_on_empty_exam_pages"]:
            entry["referenced_on_empty_exam_pages"].append(ref_page)
            entry["referenced_on_answer_labels"].append(ref_answer)

    refs = sorted(
        refs_by_label.values(),
        key=lambda r: (r["drawn_on_empty_exam_page"], r["figure_label"]),
    )
    for entry in refs:
        entry["referenced_on_empty_exam_pages"].sort()
        entry["referenced_on_answer_labels"].sort()
    return attachments, refs


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


# ---------------------------------------------------------------------------
# Pass 2: parent stems
# ---------------------------------------------------------------------------

def _compute_parent_attachments(
    questions: list[dict],
    empty_exam_has_cover: bool,
) -> tuple[_AttachMap, list[dict]]:
    """Resolve parent → child cross-page stems into (attachments, refs).

    For every child question whose page is later than an ancestor's, attach
    the ancestor's page to the child's marking call. Walks the full ancestor
    chain so a 3-level case (Q9 p8 → 9a p9 → 9ai p10) attaches *both* p8 and
    p9 to 9ai's call.
    """
    walk = list(_walk_parent_attachments(questions))

    attachments: _AttachMap = {}
    refs_by_parent: dict[tuple[str, int], dict] = {}
    seen_pairs: set[tuple[int, int, str]] = set()
    for child_page, ancestor_page, ancestor_number, child_number in walk:
        child_answer = _exam_page_to_answer_label(child_page, empty_exam_has_cover)
        ancestor_answer = _exam_page_to_answer_label(ancestor_page, empty_exam_has_cover)
        if child_answer is None or ancestor_answer is None:
            continue
        source = f"cross_page_parent_{ancestor_number}"
        key = (child_answer, ancestor_answer, source)
        if key not in seen_pairs:
            attachments.setdefault(child_answer, []).append((ancestor_answer, source))
            seen_pairs.add(key)
        entry = refs_by_parent.setdefault((ancestor_number, ancestor_page), {
            "parent_number": ancestor_number,
            "parent_empty_exam_page": ancestor_page,
            "parent_answer_label": ancestor_answer,
            "child_numbers": [],
            "child_empty_exam_pages": [],
            "child_answer_labels": [],
        })
        if child_number not in entry["child_numbers"]:
            entry["child_numbers"].append(child_number)
            entry["child_empty_exam_pages"].append(child_page)
            entry["child_answer_labels"].append(child_answer)

    refs = sorted(
        refs_by_parent.values(),
        key=lambda r: (r["parent_empty_exam_page"], r["parent_number"]),
    )
    return attachments, refs


def _walk_parent_attachments(
    questions: list[dict],
    ancestors: tuple[tuple[str, int], ...] = (),
) -> Iterator[tuple[int, int, str, str]]:
    """Yield (child_page, ancestor_page, ancestor_number, child_number) for every
    question whose page is later than an ancestor's page.

    Recurses into ``subquestions`` while threading the ancestor chain. Each
    yield = one attachment to make. Consumers dedup.
    """
    for q in questions:
        page = q.get("page")
        number = q.get("number")
        if isinstance(page, int) and number is not None:
            child_number = str(number)
            for a_number, a_page in ancestors:
                if page > a_page:
                    yield page, a_page, a_number, child_number
        next_ancestors = ancestors
        if isinstance(page, int) and number is not None:
            next_ancestors = ancestors + ((str(number), page),)
        for sub in q.get("subquestions") or []:
            yield from _walk_parent_attachments([sub], next_ancestors)


# ---------------------------------------------------------------------------
# Shared per-student attach loop
# ---------------------------------------------------------------------------

def _apply_attachments(
    register: dict,
    attachments: _AttachMap,
    empty_exam_has_cover: bool,
    *,
    bypass_skipped: bool,
) -> None:
    """Apply *attachments* in-place to each student's calls in *register*.

    *bypass_skipped* controls whether pages the student left blank
    (``skipped_scan_pages``) are eligible as extras. The figure pass passes
    ``False`` (a blank page in the student's scan is a useless figure
    source); the parent pass passes ``True`` (parent pages are typically
    printed exam content like flowcharts that the student doesn't write on).
    """
    if not attachments:
        return
    for student in register["students"]:
        skipped = set(student.get("skipped_scan_pages") or [])
        # Map answer_label → primary_scan_page within this student's calls,
        # then layer on skipped pages via their detected page number so a
        # blank-but-required page can still be referenced as a cross-page
        # extra. This replaces the older p_label-arithmetic lookup which
        # silently broke whenever the scan was physically misordered (the
        # detected page number, not the scan position, is the routing key
        # post-A1).
        scan_by_answer: dict[int, int] = {
            c["answer_label"]: c["primary_scan_page"] for c in student["calls"]
        }
        for call in student["calls"]:
            x = call["answer_label"]
            attach_list = attachments.get(x)
            if not attach_list:
                continue
            existing_extras = list(call["extra_scan_pages"])
            existing_sources = list(call["extra_sources"])
            for y, source in sorted(attach_list):
                scan_page_y = scan_by_answer.get(y)
                if scan_page_y is None:
                    continue   # this exam page is absent from the student's scan
                if scan_page_y == call["primary_scan_page"]:
                    continue
                if scan_page_y in existing_extras:
                    continue
                if scan_page_y in skipped and not bypass_skipped:
                    continue
                existing_extras.append(scan_page_y)
                existing_sources.append(source)
            call["extra_scan_pages"] = existing_extras
            call["extra_sources"] = existing_sources
            call["scan_pages"] = [call["primary_scan_page"]] + existing_extras


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

