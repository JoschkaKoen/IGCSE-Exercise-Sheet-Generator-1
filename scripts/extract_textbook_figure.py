#!/usr/bin/env python3
"""Extract one textbook figure as web + print crops (dual-resolution + vector PDF).

Each figure yields THREE files sharing the stem ``<NN>-p<PDFpage>-<slug>``:
  - ``<stem>.png``        — WEB raster at --web-dpi (default 300). What the handout
                            markdown references; small + API-readable for verification.
  - ``<stem>.print.png``  — PRINT raster at --print-dpi (default 600), for printing.
  - ``<stem>.pdf``        — vector crop (resolution-independent), for the print/LaTeX path.

Deterministic caption-anchored crop (no AI on the main path):
  1. Locate the ``▲ Figure N.M`` caption line on the page (the label line, not an
     in-text mention).
  2. Clamp a vertical band between the body-prose line *above* the figure and the
     caption line *below* it.
  3. Union the figure's own drawings + images + label words within that band
     (dropping the full-page background rect), add a small margin → crop rect.
  4. Crop the same rect to all three outputs above.

Fallback ``--qwen``: when a caption is not locatable as text (e.g. it lives inside
a rasterised image), seed the region with the xscore graphics detector
(``qwen3-vl-plus``) and run the same band refinement. Expected to be rare.

The vector-PDF recipe is replicated (not imported) from
``xscore.scaffold.scaffold_xml._extract_scheme_graphics`` — that helper is private,
bundles the PNG write, and hard-codes mark-scheme filenames. Textbook pages are
unrotated, so the rotation branch there is unnecessary here.

Usage:
  PYTHONPATH=<repo> .venv/bin/python scripts/extract_textbook_figure.py \\
      "BOOK.pdf" --page-index 333 --caption "Figure 20.10" \\
      --out-dir output/eXam/handouts/a_level_physics/assets \\
      --handout 20 --slug flemings-left-hand-rule
  batch:    --manifest <file.yaml>   (rows of {page_index, caption|rect, slug})
  optional: --rect x0,y0,x1,y1       crop an exact PDF-point rect (skip auto-location)
            --qwen                   vision fallback to seed the region
            --web-dpi 300 --print-dpi 600
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import fitz

# A "wide" text line (fills the column) is body prose and marks a band boundary;
# short lines (figure callouts, axis labels) are not. Tuned on the Cambridge
# Physics/CS books (text column ≈ 360 pt wide).
_PROSE_MIN_WIDTH = 220.0
# Max vertical gap (pt) between two lines of the same paragraph. A larger gap
# means whitespace before the figure starts — so we stop absorbing prose there.
_PARA_LINE_GAP = 6.0
# If no prose line sits above the caption, cap how tall a single figure may be.
_MAX_FIG_HEIGHT_PT = 360.0


def _text_lines(page: fitz.Page) -> list[dict]:
    """Every non-empty text line on the page, sorted top→bottom, with bbox."""
    out: list[dict] = []
    for blk in page.get_text("dict")["blocks"]:
        for ln in blk.get("lines", []):
            txt = "".join(s["text"] for s in ln["spans"]).strip()
            if not txt:
                continue
            xs0 = [s["bbox"][0] for s in ln["spans"]]
            xs1 = [s["bbox"][2] for s in ln["spans"]]
            ys0 = [s["bbox"][1] for s in ln["spans"]]
            ys1 = [s["bbox"][3] for s in ln["spans"]]
            x0, x1 = min(xs0), max(xs1)
            out.append({"y0": min(ys0), "y1": max(ys1), "x0": x0,
                        "width": x1 - x0, "text": txt})
    return sorted(out, key=lambda r: r["y0"])


def _is_fig_label(text: str) -> bool:
    return bool(re.match(r"^[▲\s]*(Figure|Fig\.?)\s+\d", text))


def find_caption_line(page: fitz.Page, caption: str) -> dict | None:
    """The ``▲ Figure N.M <title>`` label line for *caption* (e.g. "Figure 20.10").

    Prefers a ``▲``-prefixed line; else a line that *starts* with the label and
    carries title text after the number (so an in-text "…see Figure 20.10)." is
    not mistaken for the caption.
    """
    num = caption.strip().split()[-1]  # "20.10"
    lines = _text_lines(page)
    triangle = [
        ln for ln in lines
        if re.match(r"^▲\s*(Figure|Fig\.?)\s+" + re.escape(num) + r"\b", ln["text"])
    ]
    if triangle:
        return triangle[0]
    labelled = [
        ln for ln in lines
        if re.match(r"^(Figure|Fig\.?)\s+" + re.escape(num) + r"\b", ln["text"])
        and len(ln["text"]) > len(caption) + 8  # has a title, not just "Figure 20.10)."
    ]
    return labelled[0] if labelled else None


def _all_caption_tops(page: fitz.Page) -> list[float]:
    """y0 of every ``▲ Figure``/``Figure N`` label line on the page."""
    return [ln["y0"] for ln in _text_lines(page) if _is_fig_label(ln["text"])]


def band_from_caption(page: fitz.Page, caption_top: float) -> tuple[float, float]:
    """Vertical [top, bottom] band holding the figure above *caption_top*.

    Top edge = bottom of the body-prose paragraph above the figure. Body prose is
    a wide (> _PROSE_MIN_WIDTH pt), column-left-flush line — distinct from a
    figure's own short callouts ("thuMb", "Field"), which are narrow. After the
    last wide prose line we also skip its trailing short continuation lines (e.g.
    an in-text "…as shown in Figure 20.12." tail), so those don't leak in.

    The band is also floored by the nearest *other* figure caption above, so a
    figure stacked higher on the page isn't swept in.
    """
    lines = [ln for ln in _text_lines(page)
             if ln["y1"] <= caption_top - 2 and not _is_fig_label(ln["text"])]
    wide = [ln for ln in lines if ln["width"] > _PROSE_MIN_WIDTH and ln["x0"] < 200]
    prose_floor = None
    if wide:
        last = wide[-1]
        floor = last["y1"]
        # Absorb only the paragraph's trailing short line (e.g. an in-text
        # "…as shown in Figure 20.12." tail). Such a line follows the last full
        # line at normal line-spacing (gap < _PARA_LINE_GAP); the figure below
        # starts after a larger whitespace gap, so it is NOT absorbed.
        for ln in sorted(lines, key=lambda r: r["y0"]):
            if ln["x0"] < 200 and 0 <= ln["y0"] - floor < _PARA_LINE_GAP:
                floor = max(floor, ln["y1"])
        prose_floor = floor + 3
    other_caps = [y for y in _all_caption_tops(page) if y < caption_top - 2]
    cap_floor = (max(other_caps) + 12) if other_caps else None  # caption sits below its figure
    cands = [v for v in (prose_floor, cap_floor) if v is not None]
    top = max(cands) if cands else (caption_top - _MAX_FIG_HEIGHT_PT)
    return top, caption_top - 3


# Page furniture lives in the outer margins: the vertical running-header text and
# its dotted rule hug the right edge; the page number sits at the very bottom; the
# binding edge carries a thin strip. Keep a generous live area and drop anything
# centred outside it. (Books vary, so these are fractions of page size.)
_MARGIN_L = 0.045   # left binding strip
_MARGIN_R = 0.075   # right running-header column (≈ x>552 on a 597-pt page)
_MARGIN_B = 0.045   # bottom page-number band


def union_in_band(page: fitz.Page, top: float, bottom: float) -> fitz.Rect | None:
    """Union of drawings + images + words whose centre lies in [top, bottom] and
    inside the page's live area (margins excluded so running headers / page
    numbers / binding strips are not swept in)."""
    W, H = page.rect.width, page.rect.height
    live_l, live_r, live_b = _MARGIN_L * W, (1 - _MARGIN_R) * W, (1 - _MARGIN_B) * H
    r = fitz.Rect(1e9, 1e9, -1e9, -1e9)
    found = False

    def consider(b: fitz.Rect) -> None:
        nonlocal r, found
        cx, cy = (b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2
        if not (top - 2 <= cy <= bottom + 2):
            return
        if not (live_l <= cx <= live_r) or cy > live_b:
            return  # margin furniture (running header, page number, binding)
        if b.x0 < -1 or b.y0 < -1 or b.x1 > W + 1 or b.y1 > H + 1:
            return  # page-bleed background rect
        if b.width >= W * 0.92 or b.height >= H * 0.92:
            return  # full-page background
        if b.width < 1 or b.height < 1:
            return
        r |= b
        found = True

    for d in page.get_drawings():
        consider(d["rect"])
    for im in page.get_image_info():
        b = fitz.Rect(im["bbox"])
        if b.width >= 20 and b.height >= 20:  # skip tiny icons; keep figure photos
            consider(b)
    for w in page.get_text("words"):
        consider(fitz.Rect(w[:4]))

    if not found:
        return None
    # Clamp to band and live area so margins can't sneak back via a wide element.
    r.y0 = max(r.y0, top)
    r.y1 = min(r.y1, bottom)
    r.x0 = max(r.x0, live_l)
    r.x1 = min(r.x1, live_r)
    return r


def crop(doc: fitz.Document, page_index: int, rect: fitz.Rect, *,
         out_png: Path, out_print_png: Path, out_pdf: Path,
         web_dpi: int, print_dpi: int) -> None:
    """Write three crops of *rect*: a web PNG (web_dpi), a print PNG (print_dpi),
    and a resolution-independent vector PDF.

    The web PNG is what the handout markdown references — smaller, fast to load,
    and small enough to read back through the image API (a 600-DPI crop is
    ~3000 px and exceeds the API's per-image limit). The print PNG + vector PDF
    are the print-quality assets for the future PDF/LaTeX path.
    """
    page = doc[page_index]
    page.get_pixmap(matrix=fitz.Matrix(web_dpi / 72, web_dpi / 72),
                    colorspace=fitz.csRGB, clip=rect).save(str(out_png))
    page.get_pixmap(matrix=fitz.Matrix(print_dpi / 72, print_dpi / 72),
                    colorspace=fitz.csRGB, clip=rect).save(str(out_print_png))
    # Vector PDF crop — replicated core of _extract_scheme_graphics (no rotation).
    try:
        with fitz.open() as out:
            np = out.new_page(width=rect.width, height=rect.height)
            np.show_pdf_page(np.rect, doc, page_index, clip=rect)
            out.save(str(out_pdf), garbage=4, deflate=True, clean=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! vector PDF crop failed ({exc.__class__.__name__}) — PNGs only")


def _qwen_seed_rect(book_pdf: Path, page_index: int, caption: str) -> fitz.Rect | None:
    """Fallback: seed the figure region via the xscore qwen3-vl-plus detector."""
    from eXercise.env_load import load_project_env
    load_project_env()
    from xscore.scaffold.formats import get_scaffold_format
    from xscore.scaffold.scaffold_graphics import detect_scheme_graphics
    import tempfile

    with fitz.open(str(book_pdf)) as src, fitz.open() as one:
        one.insert_pdf(src, from_page=page_index, to_page=page_index)
        W, H = one[0].rect.width, one[0].rect.height
        tmp = Path(tempfile.mkdtemp()) / "page.pdf"
        one.save(str(tmp))
    fmt = get_scaffold_format()
    scaffold = fmt.build_scheme_scaffold([{"number": "1"}])
    by_qnum, _ = detect_scheme_graphics(tmp, scaffold, artifact_dir=None, fmt=fmt)
    boxes = by_qnum.get("1") or []
    if not boxes:
        return None
    # Pick the box best matching the wanted figure: largest by area (heuristic).
    g = max(boxes, key=lambda b: (b["x1"] - b["x0"]) * (b["y1"] - b["y0"]))
    return fitz.Rect(g["x0"] * W, g["y0"] * H, g["x1"] * W, g["y1"] * H)


def _exclude_caption_y1(page: fitz.Page, rect: fitz.Rect) -> float:
    """A ``y1`` for *rect* that excludes the figure's own ``▲ Figure N.M`` caption
    line, if one sits in the rect's lower half.

    The caption-anchored path already excludes the caption (band bottom = caption
    top). Manual ``--rect`` rows do not, and in practice they almost always reach
    down into the caption (it sits just below the figure). This guards rect mode
    against that slip. Only a caption line that horizontally overlaps the rect and
    *starts below the rect's vertical midpoint* is trimmed, so figure-internal
    labels and sub-panel tags ("a)", "b)") are never affected.
    """
    mid = rect.y0 + 0.5 * rect.height
    cap_tops = [
        ln["y0"] for ln in _text_lines(page)
        if _is_fig_label(ln["text"])
        and ln["x0"] < rect.x1 and ln["x0"] + ln["width"] > rect.x0
        and mid <= ln["y0"] < rect.y1 + 1
    ]
    return (min(cap_tops) - 3) if cap_tops else rect.y1


def resolve_rect(doc: fitz.Document, page_index: int, *, caption: str | None,
                 rect_str: str | None, use_qwen: bool, book_pdf: Path) -> fitz.Rect | None:
    """Resolve the crop rect for one figure via rect override / caption / qwen."""
    page = doc[page_index]
    if rect_str:
        rect = fitz.Rect(*[float(v) for v in rect_str.split(",")])
        trimmed = _exclude_caption_y1(page, rect)
        if trimmed < rect.y1 - 0.5:
            print(f"  [rect override] caption-trim y1 {rect.y1:.1f} -> {trimmed:.1f}")
            rect.y1 = trimmed
        print(f"  [rect override] {tuple(round(v, 1) for v in rect)}")
        return rect
    if use_qwen:
        seed = _qwen_seed_rect(book_pdf, page_index, caption or "")
        if seed is None:
            print("  ! qwen returned no graphic", file=sys.stderr)
            return None
        rect = union_in_band(page, seed.y0, seed.y1) or seed
        print(f"  [qwen seed] {tuple(round(v,1) for v in seed)} -> {tuple(round(v,1) for v in rect)}")
        return rect
    if not caption:
        print("  ! need caption, rect, or qwen", file=sys.stderr)
        return None
    cap = find_caption_line(page, caption)
    if cap is None:
        print(f"  ! caption {caption!r} not found on page index {page_index} "
              f"— try rect or qwen", file=sys.stderr)
        return None
    top, bottom = band_from_caption(page, cap["y0"])
    rect = union_in_band(page, top, bottom)
    if rect is None:
        print("  ! no figure elements in band — try rect", file=sys.stderr)
        return None
    W = page.rect.width
    mx = 0.01 * W  # small margin, clamped so it can't re-grab caption/prose
    rect = fitz.Rect(max(0, rect.x0 - mx), max(top, rect.y0 - 2),
                     min(W, rect.x1 + mx), min(bottom, rect.y1 + 2))
    print(f"  [caption] {caption!r} cap_top={cap['y0']:.1f} band=[{top:.1f},{bottom:.1f}]")
    return rect


def extract_one(doc: fitz.Document, *, page_index: int, caption: str | None,
                rect_str: str | None, use_qwen: bool, book_pdf: Path,
                out_dir: Path, handout: str, slug: str,
                web_dpi: int, print_dpi: int) -> dict | None:
    """Resolve + crop one figure; returns a meta dict (for the figures: block) or None."""
    page = doc[page_index]
    pdf_page = page_index + 1
    rect = resolve_rect(doc, page_index, caption=caption, rect_str=rect_str,
                        use_qwen=use_qwen, book_pdf=book_pdf)
    if rect is None:
        return None
    # Leak check: out-of-band prose words inside the rect (surface for the read-back).
    leak = [w[4] for w in page.get_text("words")
            if fitz.Rect(w[:4]).intersects(rect) and not _is_fig_label(w[4])
            and (w[1] < rect.y0 - 1 or w[3] > rect.y1 + 1)]
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{handout}-p{pdf_page}-{slug}"
    out_png = out_dir / f"{stem}.png"            # web (web_dpi) — referenced by the handout
    out_print_png = out_dir / f"{stem}.print.png"  # print (print_dpi)
    out_pdf = out_dir / f"{stem}.pdf"            # print (vector)
    crop(doc, page_index, rect, out_png=out_png, out_print_png=out_print_png,
         out_pdf=out_pdf, web_dpi=web_dpi, print_dpi=print_dpi)
    print(f"  [wrote] {out_png.name} ({out_png.stat().st_size // 1024} KB web) "
          f"+ .print.png ({out_print_png.stat().st_size // 1024} KB) + .pdf"
          f"  rect=[{rect.x0:.1f},{rect.y0:.1f},{rect.x1:.1f},{rect.y1:.1f}]"
          + (f"  ⚠ leak={leak}" if leak else ""))
    return {"file": f"{out_dir.name}/{stem}.png", "figure_ref": (caption or "").replace("Figure ", ""),
            "source_page_pdf": pdf_page,
            "crop_rect_pts": [round(rect.x0, 1), round(rect.y0, 1), round(rect.x1, 1), round(rect.y1, 1)],
            "slug": slug}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("book_pdf", type=Path)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--web-dpi", type=int, default=300,
                    help="DPI for the web PNG the handout references (default 300; API-readable)")
    ap.add_argument("--print-dpi", type=int, default=600,
                    help="DPI for the .print.png print asset (default 600)")
    # batch mode
    ap.add_argument("--manifest", type=Path,
                    help="YAML/JSON list of {page_index, caption|rect, slug} rows for one handout")
    ap.add_argument("--handout", help="handout number, e.g. 20 (single mode or manifest default)")
    # single mode
    ap.add_argument("--page-index", type=int, help="0-based PDF page index")
    ap.add_argument("--caption", help='e.g. "Figure 20.10"')
    ap.add_argument("--slug", help="short kebab slug")
    ap.add_argument("--rect", help="x0,y0,x1,y1 in PDF points — crop exactly this")
    ap.add_argument("--qwen", action="store_true", help="vision fallback to seed the region")
    args = ap.parse_args(argv)

    doc = fitz.open(str(args.book_pdf))

    if args.manifest:
        import yaml as _yaml
        rows = _yaml.safe_load(args.manifest.read_text(encoding="utf-8"))
        if isinstance(rows, dict):  # allow {handout: NN, figures: [...]}
            args.handout = args.handout or str(rows.get("handout"))
            rows = rows.get("figures") or []
        metas = []
        for i, row in enumerate(rows, 1):
            handout = str(row.get("handout") or args.handout)
            print(f"[{i}/{len(rows)}] handout {handout}  page_idx {row.get('page_index')}  "
                  f"{row.get('caption') or row.get('rect')}")
            m = extract_one(
                doc, page_index=int(row["page_index"]), caption=row.get("caption"),
                rect_str=row.get("rect"), use_qwen=bool(row.get("qwen")),
                book_pdf=args.book_pdf, out_dir=args.out_dir, handout=handout,
                slug=row["slug"], web_dpi=args.web_dpi, print_dpi=args.print_dpi)
            if m:
                metas.append(m)
        print(f"\n[done] {len(metas)}/{len(rows)} figures extracted")
        return 0 if len(metas) == len(rows) else 1

    if args.page_index is None or not args.handout or not args.slug:
        ap.error("single mode needs --page-index, --handout, --slug")
    m = extract_one(doc, page_index=args.page_index, caption=args.caption,
                    rect_str=args.rect, use_qwen=args.qwen, book_pdf=args.book_pdf,
                    out_dir=args.out_dir, handout=args.handout, slug=args.slug,
                    web_dpi=args.web_dpi, print_dpi=args.print_dpi)
    return 0 if m else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
