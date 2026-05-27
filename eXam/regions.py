"""Lazy answer-region detection for the eXam practice page.

The ``regions.json`` sidecar caches the heuristic detector's output keyed by
``DETECTOR_VERSION`` and the snippet PDF's content hash. Improving the detector
only requires bumping the version constant — every cached file regenerates on
the next student view, no migration code.

Lives next to ``question.pdf`` under
``output/eXam/bank/<subject>/<paper_stem>/<qnum>/regions.json``.

Heavy deps (``fitz``, ``xscore.scaffold.pdf_parser.*``) are lazy-imported inside
the functions that need them so the web layer's startup cost stays flat.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

DETECTOR_VERSION = 4

# Capture-group variants of the structural-label patterns in
# xscore.scaffold.pdf_parser.answer_fields._RE_STRUCTURAL_NEXT. Used to extract
# the on-page label from a matching text line so we can compare it to the
# expected leaf-derived label.
#
# The optional ``\d{1,2}\s+`` prefix handles Cambridge's compound-opener pattern
# where the first sub-label is fused with the question number on one line, e.g.
# "10 (a) Nerve impulses can be transmitted…".
_LETTER_LABEL = re.compile(r"^\s*(?:\d{1,2}\s+)?\(?([a-z])\)\s*", re.IGNORECASE)
_ROMAN_LABEL = re.compile(r"^\s*(?:\d{1,2}\s+)?\(([ivxlcdm]+)\)\s*", re.IGNORECASE)


def _leaf_chain(top_num: str, leaf_num: str) -> list[str]:
    """Per-level labels for *leaf_num* below *top_num*.

    ``("1", "1a")    → ["a"]``
    ``("1", "1bii")  → ["b", "ii"]``
    ``("1", "1")     → []``  (top-level leaf, no subpart)
    Mirrors ``eXam.runtime._leaf_suffix`` but returns the parts as a list.
    """
    if leaf_num == top_num:
        return []
    tail = leaf_num[len(top_num):]
    m = re.match(r"^([a-z])([ivx]+)?$", tail, re.IGNORECASE)
    if not m:
        return [tail]
    letter, roman = m.groups()
    return [letter] + ([roman] if roman else [])


def _expected_label_sequence(
    top_num: str, leaves: list[dict]
) -> list[tuple[str, int]]:
    """Derive ``(label, target_leaf_index)`` pairs in document order.

    For leaves ``[1a, 1bi, 1bii, 1biii]`` returns
    ``[("a", 0), ("b", 1), ("i", 1), ("ii", 2), ("iii", 3)]`` — a parent
    label like ``"b"`` advances the cursor to the first leaf of that branch.
    """
    out: list[tuple[str, int]] = []
    prev: list[str] = []
    for idx, leaf in enumerate(leaves):
        chain = _leaf_chain(top_num, str(leaf.get("number", "")))
        common = 0
        while common < len(prev) and common < len(chain) and prev[common] == chain[common]:
            common += 1
        for part in chain[common:]:
            out.append((part, idx))
        prev = chain
    return out


def _walk_label_matches(page, expected: list[tuple[str, int]]) -> list[tuple[float, int]]:
    """Walk page text lines top-down; return ``(y0, leaf_idx)`` for each
    matched expected label, in order.

    Handles compound labels on a single line — Cambridge frequently writes
    "(a) (i) Decide the…" as one line opening both the (a) and (a)(i) levels.
    After matching the first label, the remainder of the line is re-scanned
    for further labels until the cursor stalls.

    Inline references like "Use the data from (a)…" can't accidentally advance
    the cursor because they appear later in their line, not at line start.
    """
    if not expected:
        return []
    lines: list[tuple[float, str]] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            bbox = line.get("bbox") or (0, 0, 0, 0)
            text = "".join(span.get("text", "") for span in line.get("spans", []))
            lines.append((float(bbox[1]), text))
    lines.sort(key=lambda r: r[0])

    matched: list[tuple[float, int]] = []
    cursor = 0
    for y0, text in lines:
        if cursor >= len(expected):
            break
        remaining = text
        while cursor < len(expected):
            want, leaf_idx = expected[cursor]
            m = _LETTER_LABEL.match(remaining) or _ROMAN_LABEL.match(remaining)
            if m and m.group(1).lower() == want.lower():
                matched.append((y0, leaf_idx))
                cursor += 1
                remaining = remaining[m.end():]
            else:
                break
    return matched


def _region_to_dict(wa, leaf_number: str, field_idx: int, confident: bool) -> dict:
    return {
        "field_id": f"{leaf_number}__{field_idx}",
        "leaf_number": leaf_number,
        "kind": wa.kind,
        "page": wa.bbox.page,
        "x0": float(wa.bbox.x0),
        "y0": float(wa.bbox.y0),
        "x1": float(wa.bbox.x1),
        "y1": float(wa.bbox.y1),
        "assignment_confident": confident,
    }


def _assign_regions_to_leaves(
    writing_areas: list, leaves: list[dict], top_num: str, page
) -> list[dict]:
    """Map each detected region to a leaf number.

    Single-leaf questions: every region gets the sole leaf number.
    Multi-leaf: walk the label sequence and assign each region to the leaf
    whose matched label-y sits immediately above it. Regions that fall before
    any matched label go to leaf 0 with ``assignment_confident=False``.
    """
    if not writing_areas or not leaves:
        return []

    if len(leaves) == 1:
        leaf_num = str(leaves[0].get("number", ""))
        out: list[dict] = []
        for idx, wa in enumerate(
            sorted(writing_areas, key=lambda w: (w.bbox.page, w.bbox.y0, w.bbox.x0))
        ):
            out.append(_region_to_dict(wa, leaf_num, idx, confident=True))
        return out

    expected = _expected_label_sequence(top_num, leaves)
    matched = _walk_label_matches(page, expected)
    all_labels_matched = len(matched) == len(expected)

    per_leaf_count: dict[str, int] = {}
    out = []
    for wa in sorted(writing_areas, key=lambda w: (w.bbox.page, w.bbox.y0, w.bbox.x0)):
        assigned_idx = 0
        used_fallback = True
        for y0, idx in matched:
            if y0 <= wa.bbox.y0:
                assigned_idx = idx
                used_fallback = False
            else:
                break
        leaf_num = str(leaves[assigned_idx].get("number", ""))
        n = per_leaf_count.get(leaf_num, 0)
        per_leaf_count[leaf_num] = n + 1
        out.append(_region_to_dict(
            wa, leaf_num, n,
            confident=all_labels_matched and not used_fallback,
        ))
    return out


def _detect_on_snippet(snippet_pdf: Path) -> tuple[list, Any, float, float, Any]:
    """Run the heuristic detector on a single-question snippet PDF.

    Returns ``(writing_areas, page, pdf_w, pdf_h, doc)``. Caller closes ``doc``.

    NOTE: ``ParserConfig`` margin-band knobs assume A4 dimensions; very tall
    snippets (e.g. q3 at ~1635pt ≈ 1.94× A4 height) may shift some thresholds.
    Empirically still produces sensible regions, but rerun
    ``scripts/verify_writing_areas_snapshot.py`` against snippets if calibration
    regressions show up.
    """
    import fitz  # noqa: PLC0415
    from xscore.scaffold.pdf_parser.config import ParserConfig
    from xscore.scaffold.pdf_parser.writing_areas import _detect_in_region
    from xscore.shared.models import BBox, Question

    doc = fitz.open(snippet_pdf)
    page = doc[0]
    rect = page.rect
    region = BBox(0.0, 0.0, float(rect.width), float(rect.height), page=1)
    q = Question(
        number="0",
        question_type="short_answer",
        text="",
        marks=0,
        bbox=region,
        continuation_bboxes=[],
        subquestions=[],
    )
    cfg = ParserConfig()
    areas = _detect_in_region(doc, cfg, q, region, run_eq_blank_pattern=True)
    return areas, page, float(rect.width), float(rect.height), doc


def _atomic_write_json(path: Path, payload: dict) -> None:
    """Tempfile in same dir + ``os.replace`` → POSIX-atomic publish.

    Two concurrent writers produce byte-identical output (detector is pure),
    so last-write-wins is safe; readers either see the old file or the new
    one, never a partial.
    """
    tmp = path.with_suffix(f".json.tmp.{uuid4().hex[:8]}")
    tmp.write_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _find_question_entry(yaml_data: dict, qnum: str) -> dict | None:
    for q in yaml_data.get("questions", []):
        if isinstance(q, dict) and str(q.get("number")) == qnum:
            return q
    return None


def _is_pure_mcq(entry: dict) -> bool:
    return (
        isinstance(entry, dict)
        and entry.get("question_type") == "multiple_choice"
        and not (entry.get("subquestions") or [])
    )


def _empty_payload(snippet_sha: str, pdf_w: float, pdf_h: float, *, is_mcq: bool) -> dict:
    return {
        "detector_version": DETECTOR_VERSION,
        "snippet_sha": snippet_sha,
        "pdf_width_pt": pdf_w,
        "pdf_height_pt": pdf_h,
        "is_mcq": is_mcq,
        "regions": [],
    }


def ensure_question_regions(question_id: str) -> Path:
    """Return path to up-to-date ``regions.json`` for *question_id*.

    Generates lazily on first call (and on detector-version bump / snippet-hash
    change). Raises ``FileNotFoundError`` if the snippet PDF doesn't exist —
    callers should map to HTTP 404 (matches ``/eXam/practice/pdf/`` semantics).
    """
    from eXam.bank import _file_sha
    from eXam.runtime import (
        _collect_leaves, _load_paper_yaml, parse_question_id, pdf_path_for,
    )

    snippet_path = pdf_path_for(question_id)
    if not snippet_path.exists():
        raise FileNotFoundError(f"Snippet not found: {snippet_path}")

    regions_path = snippet_path.with_name("regions.json")
    snippet_sha = _file_sha(snippet_path)

    if regions_path.exists():
        try:
            cached = json.loads(regions_path.read_text(encoding="utf-8"))
            if (cached.get("detector_version") == DETECTOR_VERSION
                    and cached.get("snippet_sha") == snippet_sha):
                return regions_path
        except (json.JSONDecodeError, OSError):
            pass

    subject, paper_stem, qnum = parse_question_id(question_id)
    yaml_data = _load_paper_yaml(subject, paper_stem, "exam_questions.yaml")
    question_entry = _find_question_entry(yaml_data, qnum)

    import fitz  # noqa: PLC0415
    with fitz.open(snippet_path) as probe:
        rect = probe[0].rect
        pdf_w, pdf_h = float(rect.width), float(rect.height)

    if question_entry is None:
        _atomic_write_json(
            regions_path,
            _empty_payload(snippet_sha, pdf_w, pdf_h, is_mcq=False),
        )
        return regions_path

    if _is_pure_mcq(question_entry):
        _atomic_write_json(
            regions_path,
            _empty_payload(snippet_sha, pdf_w, pdf_h, is_mcq=True),
        )
        return regions_path

    leaves = _collect_leaves(question_entry, qnum, has_images=False)

    areas, page, pdf_w, pdf_h, doc = _detect_on_snippet(snippet_path)
    try:
        regions = _assign_regions_to_leaves(areas, leaves, qnum, page)
    finally:
        doc.close()

    payload = {
        "detector_version": DETECTOR_VERSION,
        "snippet_sha": snippet_sha,
        "pdf_width_pt": pdf_w,
        "pdf_height_pt": pdf_h,
        "is_mcq": False,
        "regions": regions,
    }
    _atomic_write_json(regions_path, payload)
    return regions_path
