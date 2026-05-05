"""Marking page register — explicit, persisted record of every AI marking call.

The register answers the question "for student S, marking answer-page P, which
scan pages will the AI see in one call?" Today this decision is computed
inline at marking time in :func:`xscore.marking.ai_mark.run_ai_marking`; this
module hoists that logic into a persisted artifact so it can be inspected,
diffed, and refined by additional steps before any expensive marking work
happens.

Lifecycle:

1. **Step 18** (build_marking_register_v1) writes the *initial* register
   (``18_build_marking_register/marking_page_register.json``) with one primary
   call per non-cover scan page that has student handwriting. No extras —
   that's all step 21's job.
2. **Step 21** (detect_cross_page_context) reads register v1 and augments it
   in three passes:
   (a) **continuation** — calls whose ``answer_label`` matches an empty-exam
       page classified as ``blank page`` or ``writing space page`` are
       removed from primary calls and re-attached as extras to the most
       recent preceding ``question page`` call.
   (b) **figures** — pages that mention figures drawn elsewhere get the
       figure's drawn-on page as an extra.
   (c) **parent stems** — pages whose questions are children of a parent on
       an earlier page get the parent's page as an extra (so the AI sees
       flowcharts, tables, or stems that introduce the sub-questions).
   Writes ``21_detect_cross_page_context/marking_page_register.json``.
3. **Step 29** (AI marking) loads the most-refined register available and
   iterates the calls. Two filters that *cannot* be baked in (scaffold-bounds
   cap and CLI cohort filter) are applied at iteration time.

Schema is documented in the project plan; see also :func:`build_initial_register`
for the source of truth.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from xscore.shared.path_builders import (
    artifact_blueprint_path,
    artifact_marking_page_register_v1_path,
    artifact_marking_page_register_v2_path,
)

if TYPE_CHECKING:
    from xscore.shared.pipeline_ctx import _Ctx


SCHEMA_VERSION = 1
REGISTER_FILENAME = "marking_page_register.json"


def _cover_offset(has_cover: bool, empty_exam_has_cover: bool) -> int:
    """How many positions to shift between scan ``p_label`` and ``answer_label``.

    Two-sided so all four (student-cover, empty-exam-cover) combinations
    map p_label → empty-exam page correctly:

    - student has cover, empty doesn't  → +1 (student's scan has an extra
      cover; subtract it to land on the right empty-exam page).
    - empty has cover, student doesn't  → -1 (student's scan is missing
      the empty cover; shift up).
    - otherwise → 0.

    ``answer_label = p_label - cover_offset``.
    """
    if has_cover and not empty_exam_has_cover:
        return 1
    if empty_exam_has_cover and not has_cover:
        return -1
    return 0


# ---------------------------------------------------------------------------
# Builder — used by step 18 (writes v1) and the step 29 backwards-compat path
# ---------------------------------------------------------------------------

def build_initial_register(ctx: "_Ctx") -> dict:
    """Build register v1 from page assignments + handwriting check.

    Pure function: reads the on-disk artifacts of steps 15 and 16 plus
    ``ctx.empty_exam_has_cover``. Returns the in-memory register dict.

    Each non-cover scan page with handwriting becomes one primary marking
    call. Continuation-page attachment (blank or writing-space pages with
    handwriting → attach to the previous question page) is applied later by
    step 21; this builder produces no extras. Scaffold-bounds cap and cohort
    filter are runtime concerns of step 29.
    """
    from xscore.shared.exam_paths import (
        artifact_exam_student_list_json_path,
        artifact_handwriting_json_path,
    )

    assert ctx.artifact_dir is not None
    list_path = artifact_exam_student_list_json_path(ctx.artifact_dir)
    raw_assignments: list[dict] = json.loads(list_path.read_text(encoding="utf-8"))

    empty_exam_has_cover = bool(ctx.empty_exam_has_cover)
    handwriting_path = artifact_handwriting_json_path(ctx.artifact_dir)
    skip_by_student = _load_handwriting_skips(handwriting_path, raw_assignments)
    detected_by_scan = _load_detected_pages(handwriting_path)

    students_out: list[dict] = []
    total_calls = 0
    for a in raw_assignments:
        student_name = a["student_name"]
        page_numbers: list[int] = a["page_numbers"]
        cover_page_number = a.get("cover_page_number")
        has_cover = cover_page_number is not None
        cover_offset = _cover_offset(has_cover, empty_exam_has_cover)
        student_skip = skip_by_student.get(student_name, set())

        calls: list[dict] = []
        for p_label, scan_page in enumerate(page_numbers, 1):
            if has_cover and p_label == 1:
                continue   # cover page never marked
            # Trust step 15's AI-detected page identity (after recheck) over
            # the physical scan position. For correctly-ordered scans the two
            # values are identical; for misordered scans (e.g. duplex back
            # sides shifted by one) the detected value is the only correct
            # routing key — without this branch, the marker is asked
            # questions that aren't on the image. Falls back to position when
            # detection was inconclusive (None) or identified the cover.
            detected = detected_by_scan.get(scan_page)
            if detected is not None and detected >= 1 and not (
                empty_exam_has_cover and detected == 1
            ):
                answer_label = detected
            else:
                answer_label = p_label - cover_offset
            if scan_page in student_skip:
                continue   # page with no handwriting
            calls.append({
                "p_label": p_label,
                "answer_label": answer_label,
                "primary_scan_page": scan_page,
                "extra_scan_pages": [],
                "extra_sources": [],
                "scan_pages": [scan_page],
            })

        page_set_anomaly = _detect_page_set_anomaly(
            calls,
            page_numbers=page_numbers,
            has_cover=has_cover,
            empty_exam_has_cover=empty_exam_has_cover,
            student_skip=student_skip,
            detected_by_scan=detected_by_scan,
        )

        student_record: dict = {
            "student_name": student_name,
            "cover_page_number": cover_page_number,
            "page_numbers": list(page_numbers),
            "answer_page_count": len(page_numbers) - (1 if has_cover else 0),
            "skipped_scan_pages": sorted(student_skip),
            "calls": calls,
        }
        if page_set_anomaly is not None:
            student_record["page_set_anomaly"] = page_set_anomaly
        students_out.append(student_record)
        total_calls += len(calls)

    if os.environ.get("MARKING_PAGE_SET_STRICT", "0") == "1":
        bad = [s["student_name"] for s in students_out if "page_set_anomaly" in s]
        if bad:
            from xscore.shared.terminal_ui import warn_line
            warn_line(
                "MARKING_PAGE_SET_STRICT=1: aborting; page-set anomalies for: "
                + ", ".join(bad)
            )
            raise SystemExit(1)

    return {
        "metadata": {
            "schema_version": SCHEMA_VERSION,
            "produced_by_step": 18,
            "produced_by_step_name": "build_marking_register_v1",
            "empty_exam_has_cover": empty_exam_has_cover,
            "applied_extras": [],
            "total_students": len(students_out),
            "total_calls": total_calls,
        },
        "students": students_out,
    }


def _load_handwriting_skips(
    handwriting_path: Path,
    raw_assignments: list[dict],
) -> dict[str, set[int]]:
    """Parse handwriting.json into a per-student set of scan pages to skip.

    A scan page is skipped iff its ``has_handwriting`` flag is ``False`` —
    a page the model is confident contains no student work. Pages where the
    flag is ``None`` (inconclusive) are NOT skipped: they fall through into
    primary marking calls so a human reviewer sees them. Per-student
    grouping is derived using the page-assignment list from step 16.
    """
    skip_by_student: dict[str, set[int]] = {}
    if not handwriting_path.exists():
        return skip_by_student

    data = json.loads(handwriting_path.read_text(encoding="utf-8"))
    by_scan: dict[int, dict] = {
        int(entry["scan_page"]): entry
        for entry in data.get("scan_pages", [])
        if entry.get("scan_page") is not None
    }

    for a in raw_assignments:
        student_name = a.get("student_name")
        if student_name is None:
            continue
        page_numbers: list[int] = a.get("page_numbers", [])
        skip: set[int] = set()
        for scan_page in page_numbers:
            entry = by_scan.get(scan_page)
            if entry is not None and entry.get("has_handwriting") is False:
                skip.add(scan_page)
        skip_by_student[student_name] = skip
    return skip_by_student


def _load_detected_pages(handwriting_path: Path) -> dict[int, int]:
    """Parse handwriting.json into a ``{scan_page: matched_page_number}`` map.

    Step 15 emits ``matched_page_number`` for every scan page after the
    classifier (and its recheck pass) settle on a page identity. Entries
    where the matcher was inconclusive are absent — caller falls back to
    the position-based ``answer_label = p_label - cover_offset`` rule.

    Cover pages have a ``cover`` page_type but no numeric ``matched_page_number``
    (the field is None for them); they're naturally excluded.
    """
    detected: dict[int, int] = {}
    if not handwriting_path.exists():
        return detected
    data = json.loads(handwriting_path.read_text(encoding="utf-8"))
    for entry in data.get("scan_pages", []):
        sp = entry.get("scan_page")
        pn = entry.get("matched_page_number")
        if isinstance(sp, int) and isinstance(pn, int):
            detected[sp] = pn
    return detected


def _detect_page_set_anomaly(
    calls: list[dict],
    *,
    page_numbers: list[int],
    has_cover: bool,
    empty_exam_has_cover: bool,
    student_skip: set[int],
    detected_by_scan: dict[int, int],
) -> dict | None:
    """Return ``{"duplicates": [...], "missing": [...]}`` or ``None`` if clean.

    A clean per-student page set means every non-cover position covers a
    distinct empty-exam page in the expected range. Anomalies arise from
    physical scan misorder, missing back-side pages (e.g. duplex pad-at-end
    masking an upstream missing page), or ambiguous page detection.

    Determined post-A1: ``calls`` already carries the trusted ``answer_label``
    (matched page number when available). Skipped pages are folded back in
    via their detected page so a "no handwriting" page still counts toward
    the cover-set check.
    """
    actual: list[int] = [c["answer_label"] for c in calls]
    for sp in student_skip:
        d = detected_by_scan.get(sp)
        if isinstance(d, int) and d >= 1:
            actual.append(d)
    seen: dict[int, int] = {}
    for v in actual:
        seen[v] = seen.get(v, 0) + 1
    duplicates = sorted(p for p, n in seen.items() if n > 1)

    answer_slots = len(page_numbers) - (1 if has_cover else 0)
    if empty_exam_has_cover:
        # cover is empty-exam page 1; non-cover pages start at 2
        expected = set(range(2, answer_slots + 2))
    else:
        expected = set(range(1, answer_slots + 1))
    actual_set = set(actual)
    missing = sorted(expected - actual_set)

    if not duplicates and not missing:
        return None
    return {"duplicates": duplicates, "missing": missing}


# ---------------------------------------------------------------------------
# Cross-page context detection (step 21) — figure references + parent stems
# ---------------------------------------------------------------------------

# Matches "Fig. 1.1", "Fig 1.1", "Figure 1.1", "Fig. 5" — case-insensitive.
_FIGURE_RE = re.compile(r"(?i)\bfig(?:ure|\.)?\s*(\d+(?:\.\d+)?)")


# An ``Attachment`` records "for answer-page x, also include answer-page y"
# along with a ``source`` string that names *why* (e.g. "cross_page_fig_1.1"
# or "cross_page_parent_9"). The per-student attach loop dedups on (y, source)
# so the same scan page can carry multiple sources if multiple detectors agree.
_AttachMap = dict[int, list[tuple[int, str]]]


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

def write_register(path: Path, register: dict) -> None:
    """Serialise *register* as pretty-printed JSON, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(register, indent=2, ensure_ascii=False), encoding="utf-8")


def load_register(artifact_dir: Path) -> dict | None:
    """Return the most-refined register available, or ``None`` if none exists.

    Tries the step 21 register first (cross-page-context-augmented), falling
    back to the step 18 register (handwriting-extras only). Includes the
    pre-rename legacy folder name as an intermediate fallback so resuming an
    old run still finds the register. Returns ``None`` when none of the
    candidates exists.
    """
    candidates = (
        artifact_marking_page_register_v2_path(artifact_dir),
        artifact_dir / "19_detect_cross_page_figures" / REGISTER_FILENAME,
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
# Iteration with runtime filters (consumed by step 29)
# ---------------------------------------------------------------------------

def iter_marking_calls(
    register: dict,
    *,
    raw_assignments: list[dict],
    scaffold_page_count: int | None = None,
    student_filter: set[str] | None = None,
    artifact_dir: Path | None = None,
    fmt_ext: str = "yaml",
) -> Iterator[tuple[dict, int, int, int, list[int], list[str]]]:
    """Yield ``(assignment, p_label, answer_label, answer_page_count,
    extra_scan_pages, extra_sources)``.

    *raw_assignments* is the parsed ``exam_student_list.json`` content; it's
    needed because the marking loop uses the original ``page_numbers`` list
    for rendering and labelling. The register knows which calls to make; the
    raw list provides the per-student pixel-level metadata.

    *extra_sources* is index-aligned with *extra_scan_pages* — entry ``i``
    names the detector that contributed page ``i`` (e.g. ``"continuation"``,
    ``"cross_page_parent_9"``, ``"cross_page_fig_1.1"``). Consumers can use
    this to render per-call provenance without re-walking the register.

    Filters applied:
    - **scaffold-bounds cap**: drops calls with
      ``answer_label > scaffold_page_count`` (only when *scaffold_page_count*
      is provided).
    - **cohort filter**: drops students whose ``student_name`` is not in
      *student_filter* (only when the set is provided; ``None`` = include all).
    - **blueprint presence**: when *artifact_dir* is provided, drops calls
      whose ``answer_label`` has no blueprint file on disk. This filters out
      structurally-redundant primary calls for empty-exam pages with no leaf
      gradable questions (parent-stem pages whose children live elsewhere) —
      step 21 has already attached such pages' scans as ``extras`` to the
      child question's call, so the primary is redundant. Without the filter,
      downstream ``bp_path.read_text()`` raises ``FileNotFoundError``.
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
            if artifact_dir is not None:
                if not artifact_blueprint_path(artifact_dir, answer_label, fmt=fmt_ext).is_file():
                    continue
            yield (
                assignment,
                call["p_label"],
                answer_label,
                answer_page_count,
                list(call["extra_scan_pages"]),
                list(call.get("extra_sources") or []),
            )


# ---------------------------------------------------------------------------
# Step-21 terminal display
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
