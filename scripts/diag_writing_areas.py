"""Probe the writing-area detector on a single paper and print per-leaf details.

Used during calibration to figure out why a specific false positive fires.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import fitz
from xscore.scaffold.pdf_parser import parse_exam_pdf
from xscore.scaffold.pdf_parser.config import DEFAULT_PARSER_CONFIG
from xscore.scaffold.pdf_parser.writing_areas import (
    _extract_vector_segments,
    _extract_text_dotted_rules,
    _text_lines_in,
    _classify_table_grid,
)
from xscore.scaffold.pdf_parser.layout import cell_for_point
from xscore.scaffold.pdf_parser.regions import clip_horizontal_bounds
from xscore.shared.models import flatten_questions


def main() -> int:
    pdf = REPO / sys.argv[1]
    leaf_filter = sys.argv[2] if len(sys.argv) > 2 else None  # e.g. "11a"

    with tempfile.TemporaryDirectory() as art:
        qs = parse_exam_pdf(pdf, exam_folder=Path(art), artifact_dir=Path(art))

    doc = fitz.open(pdf)
    try:
        for q in flatten_questions(qs):
            if q.subquestions or q.question_type == "multiple_choice":
                continue
            if leaf_filter and q.number != leaf_filter:
                continue
            pi = q.bbox.page - 1
            page = doc[pi]
            cx = (q.bbox.x0 + q.bbox.x1) * 0.5
            cy = (q.bbox.y0 + q.bbox.y1) * 0.5
            cell = cell_for_point(page, cx, cy)
            h0, h1 = clip_horizontal_bounds(doc, pi, DEFAULT_PARSER_CONFIG, cell)
            clip = fitz.Rect(h0, q.bbox.y0, h1, q.bbox.y1)

            print(
                f"\n=== leaf {q.number} (page {q.bbox.page}) "
                f"bbox=({q.bbox.x0:.1f},{q.bbox.y0:.1f},{q.bbox.x1:.1f},{q.bbox.y1:.1f}) "
                f"clip=({clip.x0:.1f},{clip.y0:.1f},{clip.x1:.1f},{clip.y1:.1f})"
            )

            h_rules, v_rules, rects = _extract_vector_segments(page, clip, DEFAULT_PARSER_CONFIG)
            h_rules.extend(_extract_text_dotted_rules(page, clip, DEFAULT_PARSER_CONFIG))
            h_rules.sort(key=lambda r: r.y)
            text_lines = _text_lines_in(page, clip)

            print(f"  h_rules ({len(h_rules)}):")
            for r in h_rules:
                print(f"    y={r.y:.1f}  x=[{r.x0:.1f},{r.x1:.1f}] len={r.length:.1f} dotted={r.dotted}")
            print(f"  v_rules ({len(v_rules)}):")
            for r in v_rules:
                print(f"    x={r.x:.1f}  y=[{r.y0:.1f},{r.y1:.1f}] len={r.length:.1f}")
            print(f"  closed_rects ({len(rects)}):")
            for rc in rects:
                print(f"    [{rc.x0:.1f},{rc.y0:.1f}]–[{rc.x1:.1f},{rc.y1:.1f}]  w={rc.x1-rc.x0:.1f} h={rc.y1-rc.y0:.1f}")
            print(f"  text_lines ({len(text_lines)}):")
            for tx0, ty0, tx1, ty1, t in text_lines[:8]:
                print(f"    y=[{ty0:.1f},{ty1:.1f}]  text={t[:60]!r}")
            if len(text_lines) > 8:
                print(f"    … +{len(text_lines)-8} more")

            print(f"  writing_areas ({len(q.writing_areas)}):")
            for wa in q.writing_areas:
                print(f"    kind={wa.kind}  bbox=({wa.bbox.x0:.1f},{wa.bbox.y0:.1f},{wa.bbox.x1:.1f},{wa.bbox.y1:.1f})")
    finally:
        doc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
