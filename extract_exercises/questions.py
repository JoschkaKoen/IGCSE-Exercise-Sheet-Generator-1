# -*- coding: utf-8 -*-
"""Locate questions and vertical regions in question papers."""

import re

from .config import MARGIN_BOTTOM, MARGIN_TOP, PADDING_ABOVE, QUESTION_X_MAX


def find_question_positions(doc):
    """Scan every page for top-level question numbers in the question paper."""
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

                if y0 < MARGIN_TOP or y0 > MARGIN_BOTTOM:
                    continue
                if x0 > QUESTION_X_MAX:
                    continue

                text = first_span["text"].strip()
                font_size = first_span["size"]

                if font_size < 9 or font_size > 13:
                    continue

                # Bare standalone number ("9") — accepted at any y position.
                # Number leading the first line of a question ("10 A shop …") — only
                # accepted near the top of the page; mid-page inline numbers (e.g.
                # "28 and 35 students…" inside a question body) are false positives.
                bare = re.match(r"^\d{1,2}$", text)
                inline = (not bare) and y0 <= MARGIN_TOP + 80 and re.match(r"^(\d{1,2})\s", text)
                m = bare or inline
                if m:
                    qnum = int(re.match(r"^(\d{1,2})", text).group(1))
                    if 1 <= qnum <= 40 and qnum not in seen:
                        seen.add(qnum)
                        positions.append((qnum, page_idx, y0))

    positions.sort(key=lambda x: (x[1], x[2]))
    return positions


def get_question_regions(doc, positions, requested_questions):
    """For each requested question, determine the crop region(s)."""
    regions = []
    page_content_bottom = MARGIN_BOTTOM

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

        start_y = max(q_y - PADDING_ABOVE, MARGIN_TOP)

        if q_page == next_page:
            end_y = min(next_y - 2, page_content_bottom)
            regions.append((qnum, q_page, start_y, end_y))
        else:
            regions.append((qnum, q_page, start_y, page_content_bottom))
            for mid_page in range(q_page + 1, next_page):
                page_text = doc[mid_page].get_text().strip()
                if "BLANK PAGE" in page_text:
                    continue
                regions.append((qnum, mid_page, MARGIN_TOP, page_content_bottom))
            if next_page > q_page:
                page_text = doc[next_page].get_text().strip()
                if "BLANK PAGE" not in page_text:
                    end_y = min(next_y - 2, page_content_bottom)
                    if end_y > MARGIN_TOP + 20:
                        regions.append((qnum, next_page, MARGIN_TOP, end_y))

    return regions
