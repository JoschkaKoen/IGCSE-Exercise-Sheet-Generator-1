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

# Step-21 cross-page extras (continuation / figures / parents) live in
# :mod:`_register_cross_page_extras`. Re-exported for backward compat.
from xscore.marking._register_cross_page_extras import (  # noqa: E402, F401
    apply_cross_page_extras,
    _apply_attachments,
    _apply_continuation_extras,
    _compute_figure_attachments,
    _compute_parent_attachments,
    _exam_page_to_answer_label,
    _walk_figure_mentions,
    _walk_parent_attachments,
)

# Backwards-compat re-exports — the human-readable summary renderers (used
# only outside the marking loop) live in :mod:`marking_page_register_summary`.
# Importers that historically pulled these symbols from here keep working.
# ---------------------------------------------------------------------------

from xscore.marking.marking_page_register_summary import (  # noqa: E402, F401
    _first_primary,
    _format_skipped_pages,
    _pretty_source_label,
    print_register_summary,
    render_cross_page_step_summary,
)
