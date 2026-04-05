# -*- coding: utf-8 -*-
"""Locate questions and vertical regions in question papers."""

from __future__ import annotations

import re

import fitz

from .config import DEFAULT_SUBJECT_CONFIG, SubjectConfig

# ---------------------------------------------------------------------------
# Trailing-whitespace / disclaimer trimming
# ---------------------------------------------------------------------------

# Text patterns that identify the Cambridge copyright/permission footer.
# Only ONE of these needs to match in a block for the entire block to be skipped.
_DISCLAIMER_TRIGGERS = (
    "permission to reproduce",
    "third-party owned material",
    "copyright acknowledgements booklet",
)

# Minimum gap (pt) between last real content and region end before we bother
# trimming.  Set equal to the strip bottom-padding so any blank beyond that
# padding is always removed.
_MIN_TRIM_GAP_PT = 4.0

# Look this many pt above the disclaimer text for a full-width separator line.
_FOOTER_MARGIN_PT = 15.0

# A drawing must be at least this wide (pt) to be treated as a footer separator.
_SEPARATOR_MIN_WIDTH_PT = 100.0


def _get_tight_y_end(page: fitz.Page, y_start: float, y_end: float) -> float:
    """Return a tighter *y_end* by removing trailing blank space and disclaimers.

    Scans the page content in ``[y_start, y_end]``:
    1. Finds the Cambridge copyright disclaimer text and the full-width
       separator line that often precedes it.  Everything from the separator
       (or disclaimer, whichever is higher) onwards is treated as footer.
    2. Finds the bottom of the last real content element (non-blank text
       block or drawing wider than 10 pt) that lies above the footer.
    3. Returns ``min(last_content_y + 8 pt, footer_start - 2 pt)`` if that
       saves more than ``_MIN_TRIM_GAP_PT`` of vertical space; otherwise
       returns *y_end* unchanged.
    """
    # ── Step 1: locate footer start (disclaimer text + optional separator) ─
    disclaimer_y = y_end  # sentinel: no disclaimer found
    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        by0 = block["bbox"][1]
        if by0 < y_start or by0 > y_end:
            continue
        flat = " ".join(
            span["text"] for line in block["lines"] for span in line["spans"]
        ).lower()
        if any(pat in flat for pat in _DISCLAIMER_TRIGGERS):
            disclaimer_y = min(disclaimer_y, by0)

    # Check for a wide horizontal separator line just above the disclaimer text.
    footer_start_y = disclaimer_y
    if disclaimer_y < y_end:
        for drawing in page.get_drawings():
            r = drawing["rect"]
            if r.width < _SEPARATOR_MIN_WIDTH_PT:
                continue
            if disclaimer_y - _FOOTER_MARGIN_PT <= r.y0 < disclaimer_y:
                footer_start_y = min(footer_start_y, r.y0)

    effective_end = min(y_end, footer_start_y - 2.0)

    # ── Step 2: last real content up to effective_end ──────────────────────
    last_y = y_start

    for block in page.get_text("dict")["blocks"]:
        if block["type"] != 0:
            continue
        by0 = block["bbox"][1]
        if by0 < y_start - 5 or by0 > effective_end:
            continue
        # Use the last *non-blank line* bottom rather than the block bbox bottom.
        # Trailing whitespace-only lines inside a block inflate the bbox without
        # containing any visible glyphs, causing over-estimation of content height.
        lines = block["lines"]
        if not lines:
            continue
        # Walk lines in reverse; skip lines whose top edge is beyond
        # effective_end (they belong to the next question's content in a
        # shared block) and skip whitespace-only lines.
        last_line_y1 = 0.0
        for line in reversed(lines):
            if line["bbox"][1] > effective_end:
                continue  # line is outside the valid scan range
            if any(s["text"].strip() for s in line["spans"]):
                last_line_y1 = min(line["bbox"][3], effective_end)
                break
        if not last_line_y1:
            continue  # no visible content within range
        last_y = max(last_y, last_line_y1)

    for drawing in page.get_drawings():
        r = drawing["rect"]
        if r.y0 < y_start - 5 or r.y0 > effective_end:
            continue
        if r.width < 10:  # skip narrow vertical borders / artefacts
            continue
        last_y = max(last_y, min(r.y1, effective_end))

    # ── Step 3: decide whether to trim ─────────────────────────────────────
    if last_y <= y_start:
        # No content found; trim to footer boundary if that saves enough.
        if footer_start_y < y_end - _MIN_TRIM_GAP_PT:
            return max(y_start, effective_end)
        return y_end

    # Cap tight at effective_end so we never spill into the footer zone.
    tight = min(last_y + 4.0, effective_end)
    if tight < y_end - _MIN_TRIM_GAP_PT:
        return tight
    return y_end


def find_question_positions(doc, cfg: SubjectConfig | None = None):
    """Scan every page for top-level question numbers in the question paper."""
    cfg = cfg or DEFAULT_SUBJECT_CONFIG
    positions = []
    seen = set()

    for page_idx in range(len(doc)):
        page = doc[page_idx]
        blocks = page.get_text("dict")["blocks"]

        for block in blocks:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                if not line["spans"]:
                    continue
                first_span = line["spans"][0]
                x0 = line["bbox"][0]
                y0 = line["bbox"][1]

                if y0 < cfg.margin_top or y0 > cfg.margin_bottom:
                    continue
                if x0 > cfg.question_x_max:
                    continue

                text = first_span["text"].strip()
                font_size = first_span["size"]

                if font_size < cfg.font_size_min or font_size > cfg.font_size_max:
                    continue

                # Bare standalone number ("9") — accepted at any y position.
                # Number leading the first line of a question ("10 A shop …") — only
                # accepted near the top of the page; mid-page inline numbers (e.g.
                # "28 and 35 students…" inside a question body) are false positives.
                bare = re.match(r"^\d{1,2}$", text)
                inline = (not bare) and y0 <= cfg.margin_top + 80 and re.match(r"^(\d{1,2})\s", text)
                m = bare or inline
                if m:
                    qnum = int(re.match(r"^(\d{1,2})", text).group(1))
                    if 1 <= qnum <= 40 and qnum not in seen:
                        seen.add(qnum)
                        positions.append((qnum, page_idx, y0))

    positions.sort(key=lambda x: (x[1], x[2]))
    return positions


def get_question_regions(doc, positions, requested_questions, cfg: SubjectConfig | None = None):
    """For each requested question, determine the crop region(s)."""
    cfg = cfg or DEFAULT_SUBJECT_CONFIG
    regions = []
    page_content_bottom = cfg.margin_bottom

    for qnum in requested_questions:
        q_entries = [p for p in positions if p[0] == qnum]
        if not q_entries:
            print(f"  Warning: Question {qnum} not found in PDF, skipping.")
            continue

        q_page, q_y = q_entries[0][1], q_entries[0][2]
        pos_idx = positions.index(q_entries[0])

        if pos_idx + 1 < len(positions):
            next_q = positions[pos_idx + 1]
            next_page, next_y = next_q[1], next_q[2]
        else:
            next_page = len(doc) - 1
            next_y = page_content_bottom
            for pi in range(len(doc) - 1, q_page - 1, -1):
                text = doc[pi].get_text().strip()
                if text and "BLANK PAGE" not in text:
                    next_page = pi
                    next_y = page_content_bottom
                    break

        start_y = max(q_y - cfg.padding_above, cfg.margin_top)

        if q_page == next_page:
            end_y = min(next_y - 2, page_content_bottom)
            end_y = _get_tight_y_end(doc[q_page], start_y, end_y)
            regions.append((qnum, q_page, start_y, end_y))
        else:
            page_end = _get_tight_y_end(doc[q_page], start_y, page_content_bottom)
            regions.append((qnum, q_page, start_y, page_end))
            for mid_page in range(q_page + 1, next_page):
                page_text = doc[mid_page].get_text().strip()
                if "BLANK PAGE" in page_text:
                    continue
                mid_end = _get_tight_y_end(doc[mid_page], cfg.margin_top, page_content_bottom)
                regions.append((qnum, mid_page, cfg.margin_top, mid_end))
            if next_page > q_page:
                page_text = doc[next_page].get_text().strip()
                if "BLANK PAGE" not in page_text:
                    end_y = min(next_y - 2, page_content_bottom)
                    end_y = _get_tight_y_end(doc[next_page], cfg.margin_top, end_y)
                    if end_y > cfg.margin_top + 20:
                        regions.append((qnum, next_page, cfg.margin_top, end_y))

    return regions
