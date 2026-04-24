"""Project scaffold bounding boxes from 4-up raw-exam PDF coordinates onto
deskewed scan page pixel coordinates.

Background
----------
The scaffold is built from a single-page 4-up PDF (595.3 x 841.9 pt) whose
four sub-pages are arranged as::

    [ sub 1 (TL) | sub 2 (TR) ]
    [ sub 3 (BL) | sub 4 (BR) ]

The deskewed scan is an A3 raster page at 300 DPI (3508 x ~4961 px).  After
deskewing it is split at the vertical midpoint into a **top half** and a
**bottom half**, each containing one landscape A4 sheet with two sub-pages
side by side.

Each sub-page header carries an "IGCSE Physics: sXX YY" label, whose center
coordinates are:
- Known exactly in 4-up PDF space (extracted from the raw vector PDF).
- Detected on the deskewed scan via template matching (stored in the
  ``*_anchors.json`` sidecar from :func:`preprocessing.deskew.deskew_pdf_raster`;
  legacy ``*_reflines.json`` is still readable).

These two pairs of corresponding points define a **similarity transform**
(uniform scale + translation) per half-page.  Rotation is already handled by
the deskew step, so no rotation term is needed.

Transform math (per half)
-------------------------
Given raw anchor pair ``(raw_L, raw_R)`` and scan anchor pair
``(scan_L, scan_R)``::

    scale = (scan_R.x − scan_L.x) / (raw_R.x − raw_L.x)
    tx    = scan_L.x − scale × raw_L.x
    ty    = mean(scan_L.y, scan_R.y) − scale × raw_L.y

    scan_x = scale × raw_x + tx
    scan_y = scale × raw_y + ty

Bbox coordinates that straddle the 4-up midpoint (y = 420.9 pt) use the
``top_transform`` when ``y0 < mid_y``, else the ``bot_transform``.

Usage example
-------------
::

    from pathlib import Path
    from scaffold.project_boxes_on_scanned_exam import (
        extract_raw_igcse_anchors,
        compute_page_transforms,
        project_scaffold_bbox,
    )

    raw_anchors  = extract_raw_igcse_anchors(Path("raw exam 4up.pdf"))
    scan_page    = reflines_data[0]   # one entry from anchors / legacy reflines sidecar
    top_tf, bot_tf = compute_page_transforms(raw_anchors, scan_page["anchors"])

    # Project a Question.bbox (BBox dataclass from shared.models)
    x0, y0, x1, y1 = project_scaffold_bbox(question.bbox, top_tf, bot_tf)
"""

from __future__ import annotations

import json
from pathlib import Path

import fitz  # PyMuPDF

from xscore.shared.models import BBox, Question, flatten_questions
from xscore.scaffold.draw_boxes_on_empty_exam import _TEAL, _YELLOW, _hsv_color
from xscore.scaffold.project_boxes_geometry import (
    _RAW_MID_X_PT, _RAW_MID_Y_PT, _PROJECTED_TRIM_LEFT_PT, _PROJECTED_RIGHT_COLUMN_UP_PT,
    _Point, SimilarityTransform,
    similarity_transform_to_dict, similarity_transform_from_dict,
    compute_half_transform, compute_page_transforms,
    _adjust_raw_bbox_for_projected_overlay, _half_page_px_to_page_rect,
    project_scaffold_bbox,
)


# ---------------------------------------------------------------------------
# Locate the vector 4-up exam PDF (IGCSE anchor geometry)
# ---------------------------------------------------------------------------

def find_raw_four_up_pdf(folder: Path) -> Path | None:
    """Return a raw exam PDF in *folder* whose name suggests a 4-up imposition.

    Projection uses :func:`extract_raw_igcse_anchors`, which expects one page with
    four quadrant headers. Skips answer keys and scans.
    """
    folder = Path(folder)
    exact = folder / "raw exam 4up.pdf"
    if exact.is_file():
        return exact
    cands = sorted(
        (
            p
            for p in folder.glob("*.pdf")
            if "4up" in p.name.lower()
            and "answer" not in p.name.lower()
            and "scan" not in p.name.lower()
        ),
        key=lambda p: p.name.lower(),
    )
    return cands[0] if cands else None


# ---------------------------------------------------------------------------
# Extract reference anchors from the raw 4-up PDF
# ---------------------------------------------------------------------------

def extract_raw_igcse_anchors(raw_4up_pdf: Path) -> dict[str, tuple[float, float]]:
    """Return the four top-of-subpage IGCSE header positions from *raw_4up_pdf*.

    The raw 4-up PDF contains "IGCSE Physics: sXX YY" labels at the top of
    each sub-page quadrant.  Some sub-pages also have a *second* scattered
    "IGCSE" line further down — those are ignored by selecting only the
    **topmost** (smallest y) label in each quadrant.

    Args:
        raw_4up_pdf: Path to the single-page 4-up PDF
            (e.g. ``"raw exam 4up.pdf"``).

    Returns:
        Dict with keys ``top_left``, ``top_right``, ``bot_left``,
        ``bot_right``; each value is ``(x_pt, y_pt)`` — center of the
        "IGCSE …" line in PDF point space.

    Raises:
        ValueError: If fewer than 4 distinct IGCSE anchor positions are found.
    """
    raw_4up_pdf = Path(raw_4up_pdf)
    igcse_centers: list[tuple[float, float, str]] = []  # (cx, cy, text)
    with fitz.open(str(raw_4up_pdf)) as doc:
        page = doc[0]
        pw, ph = page.rect.width, page.rect.height
        mid_x = pw / 2
        mid_y = ph / 2

        # Collect all IGCSE line centers, deduplicated to 1-pt grid
        seen: set[tuple[int, int]] = set()
        for block in page.get_text("dict")["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                text = "".join(s["text"] for s in line["spans"]).strip()
                if "IGCSE" not in text:
                    continue
                bb = line["bbox"]
                cx = (bb[0] + bb[2]) / 2
                cy = (bb[1] + bb[3]) / 2
                key = (round(cx), round(cy))
                if key not in seen:
                    seen.add(key)
                    igcse_centers.append((cx, cy, text))

    # For each quadrant keep only the topmost (smallest cy) label
    best: dict[str, tuple[float, float] | None] = {
        "top_left": None, "top_right": None,
        "bot_left": None, "bot_right": None,
    }
    for cx, cy, _text in igcse_centers:
        key = (
            ("top" if cy < mid_y else "bot")
            + "_"
            + ("left" if cx < mid_x else "right")
        )
        if best[key] is None or cy < best[key][1]:  # type: ignore[index]
            best[key] = (cx, cy)

    missing = [k for k, v in best.items() if v is None]
    if missing:
        raise ValueError(
            f"[project_boxes_on_scanned_exam] Could not find IGCSE anchors in {raw_4up_pdf.name} "
            f"for quadrant(s): {missing}"
        )

    return best  # type: ignore[return-value]



# ---------------------------------------------------------------------------
# Draw projected boxes on a deskewed raster scan PDF
# ---------------------------------------------------------------------------

def _projected_items_for_question_node(
    q: Question,
    color_index: int,
    top_tf: SimilarityTransform,
    bot_tf: SimilarityTransform,
    *,
    scaffold_page: int = 1,
    mid_y_pt: float = _RAW_MID_Y_PT,
) -> list[tuple[str, tuple[float, float, float, float], tuple[float, float, float], bool]]:
    """Like ``draw_boxes_on_empty_exam._rects_for_question_node`` but in projected scan space.

    Returns tuples ``(half, (x0,y0,x1,y1)_px, rgb, is_equation_blank)`` in **half-page
    pixel** coordinates (``half`` is ``\"top\"`` or ``\"bot\"`` for y-offset on the page).
    """
    out: list[tuple[str, tuple[float, float, float, float], tuple[float, float, float], bool]] = []
    color = _hsv_color(color_index)

    def add(bb: BBox | None, *, is_eq: bool = False) -> None:
        if bb is None:
            return
        if bb.page != scaffold_page:
            return
        if bb.x1 <= bb.x0 or bb.y1 <= bb.y0:
            return
        half = "top" if bb.y0 < mid_y_pt else "bot"
        quad = project_scaffold_bbox(bb, top_tf, bot_tf, mid_y_pt)
        c = _TEAL if is_eq else color
        out.append((half, quad, c, is_eq))

    add(q.bbox)
    for im in q.images:
        add(im.bbox)
    for eb in q.equation_blank_bboxes:
        add(eb, is_eq=True)
    return out


def compute_yellow_rects_for_page(
    page: fitz.Page,
    all_nodes: list[Question],
    top_tf: SimilarityTransform,
    bot_tf: SimilarityTransform,
    *,
    px_to_pt: float,
    scaffold_page: int = 1,
    mid_y_pt: float = _RAW_MID_Y_PT,
) -> list[fitz.Rect]:
    """Return yellow margin-strip rects for all exercise (non-eq) nodes on this page.

    For left-column boxes: a strip from x=0 to the box's left edge.
    For right-column boxes: a strip from the box's right edge to the page width.
    Equation-blank boxes are excluded.
    """
    h_px = int(round(page.rect.height / px_to_pt))
    mid_px = h_px // 2
    page_w = page.rect.width
    page_mid_x = page_w / 2.0
    yellow: list[fitz.Rect] = []
    for color_idx, node in enumerate(all_nodes):
        for half, quad, _color, is_eq in _projected_items_for_question_node(
            node, color_idx, top_tf, bot_tf,
            scaffold_page=scaffold_page, mid_y_pt=mid_y_pt,
        ):
            if is_eq:
                continue
            x0, y0, x1, y1 = quad
            r = _half_page_px_to_page_rect(x0, y0, x1, y1, half, mid_px, px_to_pt)
            r = r.intersect(page.rect)
            if r.is_empty:
                continue
            r_cx = (r.x0 + r.x1) / 2.0
            yr = (
                fitz.Rect(0.0, r.y0, r.x0, r.y1)
                if r_cx < page_mid_x
                else fitz.Rect(r.x1, r.y0, page_w, r.y1)
            ).intersect(page.rect)
            if not yr.is_empty:
                yellow.append(yr)
    return yellow


def overlay_projected_scaffold_on_scan_pdf(
    deskewed_pdf: Path,
    reflines_json: Path,
    raw_4up_pdf: Path,
    questions: list[Question],
    output_pdf: Path,
    *,
    dpi: int = 300,
    line_width: float = 0.9,
    scaffold_page: int = 1,
    mid_y_pt: float = _RAW_MID_Y_PT,
) -> Path:
    """Draw projected scaffold regions on a **copy** of the deskewed scan PDF.

    For each raster page, reads that page's IGCSE anchors from *reflines_json*,
    computes top/bottom similarity transforms, projects every question bbox
    (plus images and equation-blank boxes) from 4-up PDF space onto scan
    pixels, then strokes rectangles in PDF point space.  Colours follow the same
    golden-ratio scheme as :func:`scaffold.draw_boxes_on_empty_exam.write_scaffold_boxes_pdf`;
    equation blanks use teal.

    Args:
        deskewed_pdf: Output of :func:`preprocessing.deskew.deskew_pdf_raster`.
        reflines_json: Anchor sidecar (``*_anchors.json`` or legacy ``*_reflines.json``)
            with ``anchors`` per page.
        raw_4up_pdf: Raw exam PDF used to build the scaffold (4-up layout).
        questions: Root-level questions from :class:`shared.models.ExamScaffold`.
        output_pdf: Destination path (must differ from *deskewed_pdf* unless you
            intend to overwrite after loading into memory first — caller's choice).
        dpi: Rasterisation DPI of *deskewed_pdf* (default 300).
        line_width: Stroke width in PDF points.
        scaffold_page: Only draw ``BBox`` objects whose ``page`` equals this (1-based).
        mid_y_pt: 4-up split line for top vs bottom transform (default 420.9).

    Returns:
        Path to the written *output_pdf*.
    """
    deskewed_pdf = Path(deskewed_pdf)
    reflines_json = Path(reflines_json)
    raw_4up_pdf = Path(raw_4up_pdf)
    output_pdf = Path(output_pdf)

    use_tmp = output_pdf.resolve() == deskewed_pdf.resolve()
    save_path = output_pdf.with_suffix(".bbox_overlay_tmp.pdf") if use_tmp else output_pdf

    raw_anchors = extract_raw_igcse_anchors(raw_4up_pdf)
    sidecar: list[dict] = json.loads(reflines_json.read_text())
    px_to_pt = 72.0 / dpi

    all_nodes = flatten_questions(questions)

    from xscore.shared.terminal_ui import warn_line

    doc = fitz.open(str(deskewed_pdf))
    try:
        n_doc = len(doc)
        n_side = len(sidecar)
        if n_side != n_doc:
            warn_line(
                f"[bbox_overlay] sidecar has {n_side} pages, PDF has {n_doc} "
                f"— overlaying min({n_side}, {n_doc}) pages"
            )

        n_overlay = min(n_doc, n_side)
        for page_idx in range(n_overlay):
            entry = sidecar[page_idx]
            page = doc[page_idx]
            mid_px = int(round(page.rect.height / px_to_pt)) // 2
            top_tf, bot_tf = compute_page_transforms(raw_anchors, entry["anchors"])

            exercise: list[tuple[fitz.Rect, tuple[float, float, float]]] = []
            eq_blank: list[tuple[fitz.Rect, tuple[float, float, float]]] = []

            for color_idx, node in enumerate(all_nodes):
                for half, quad, color, is_eq in _projected_items_for_question_node(
                    node,
                    color_idx,
                    top_tf,
                    bot_tf,
                    scaffold_page=scaffold_page,
                    mid_y_pt=mid_y_pt,
                ):
                    x0, y0, x1, y1 = quad
                    r = _half_page_px_to_page_rect(
                        x0, y0, x1, y1, half, mid_px, px_to_pt
                    )
                    r = r.intersect(page.rect)
                    if r.is_empty:
                        continue
                    if is_eq:
                        eq_blank.append((r, color))
                    else:
                        exercise.append((r, color))

            yellow = [
                (yr, _YELLOW)
                for yr in compute_yellow_rects_for_page(
                    page, all_nodes, top_tf, bot_tf,
                    px_to_pt=px_to_pt,
                    scaffold_page=scaffold_page,
                    mid_y_pt=mid_y_pt,
                )
            ]

            for r, color in exercise + eq_blank + yellow:
                page.draw_rect(r, color=color, width=line_width)

        doc.save(str(save_path), garbage=4, deflate=True)
    finally:
        doc.close()

    if use_tmp:
        save_path.replace(output_pdf)

    return output_pdf


def write_scan_page_transforms_json(
    raw_4up_pdf: Path,
    reflines_json: Path,
    output_json: Path,
    *,
    dpi: int,
    mid_y_pt: float = _RAW_MID_Y_PT,
) -> bool:
    """Compute top/bot :class:`SimilarityTransform` per sidecar page; write JSON.

    Returns ``False`` if any page lacks scan anchors or computation fails.
    """
    raw_4up_pdf = Path(raw_4up_pdf)
    reflines_json = Path(reflines_json)
    output_json = Path(output_json)
    try:
        raw_anchors = extract_raw_igcse_anchors(raw_4up_pdf)
        sidecar: list[dict] = json.loads(reflines_json.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        from xscore.shared.terminal_ui import warn_line

        warn_line(f"Could not load data for transforms JSON: {e}")
        return False

    pages_out: list[dict] = []
    for entry in sidecar:
        try:
            top_tf, bot_tf = compute_page_transforms(raw_anchors, entry["anchors"])
        except (ValueError, KeyError, TypeError):
            from xscore.shared.terminal_ui import warn_line

            warn_line(
                "Skipping transforms JSON — incomplete scan anchors on at least one page "
                "(run pipeline through step 8, or check template matching)."
            )
            return False
        pages_out.append({
            "page": int(entry["page"]),
            "top": similarity_transform_to_dict(top_tf),
            "bot": similarity_transform_to_dict(bot_tf),
        })

    payload = {"dpi": dpi, "mid_y_pt": mid_y_pt, "pages": pages_out}
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return True


_TOP_TRIM_PT = 20.0


def _trim_first_exercise_per_subpage(
    exercise: list[tuple[fitz.Rect, tuple[float, float, float]]],
    yellow: list[tuple[fitz.Rect, tuple[float, float, float]]],
    page_width: float,
    page_height: float,
    trim_pt: float = _TOP_TRIM_PT,
) -> tuple[
    list[tuple[fitz.Rect, tuple[float, float, float]]],
    list[tuple[fitz.Rect, tuple[float, float, float]]],
]:
    """Trim *trim_pt* from the top of the first exercise box in each subpage.

    Each deskewed PDF page contains all four 4-up subpages as quadrants:
        subpage 1 (top-left)  | subpage 2 (top-right)
        subpage 3 (bot-left)  | subpage 4 (bot-right)

    The first exercise box in each quadrant tends to include a header strip
    that belongs to the subpage above, so we shave a small amount off its top.
    The paired yellow margin box is trimmed by the same amount so the two
    boxes stay vertically aligned.

    Returns new (exercise, yellow) lists; originals are not mutated.
    """
    mid_x = page_width / 2.0
    mid_y = page_height / 2.0
    exercise = list(exercise)
    yellow = list(yellow)

    quadrant_filters = [
        (lambda cx, cy: cx <  mid_x and cy <  mid_y),  # subpage 1: top-left
        (lambda cx, cy: cx >= mid_x and cy <  mid_y),  # subpage 2: top-right
        (lambda cx, cy: cx <  mid_x and cy >= mid_y),  # subpage 3: bot-left
        (lambda cx, cy: cx >= mid_x and cy >= mid_y),  # subpage 4: bot-right
    ]

    for q_filter in quadrant_filters:
        indices = [
            i for i, (r, _) in enumerate(exercise)
            if q_filter((r.x0 + r.x1) / 2.0, (r.y0 + r.y1) / 2.0)
        ]
        if not indices:
            continue
        first_idx = min(indices, key=lambda i: exercise[i][0].y0)
        r, color = exercise[first_idx]
        trimmed = fitz.Rect(r.x0, r.y0 + trim_pt, r.x1, r.y1)
        if not trimmed.is_empty:
            exercise[first_idx] = (trimmed, color)
            if first_idx < len(yellow):
                yr, yc = yellow[first_idx]
                y_trimmed = fitz.Rect(yr.x0, yr.y0 + trim_pt, yr.x1, yr.y1)
                if not y_trimmed.is_empty:
                    yellow[first_idx] = (y_trimmed, yc)

    return exercise, yellow


def overlay_projected_scaffold_from_transforms_json(
    deskewed_pdf: Path,
    transforms_json: Path,
    questions: list[Question],
    output_pdf: Path,
    *,
    boxes_json: Path | None = None,
    line_width: float = 0.9,
    scaffold_page: int = 1,
    mid_y_pt: float = _RAW_MID_Y_PT,
) -> Path | None:
    """Draw projected scaffold regions using a transforms file from step 9."""
    deskewed_pdf = Path(deskewed_pdf)
    transforms_json = Path(transforms_json)
    output_pdf = Path(output_pdf)

    try:
        payload = json.loads(transforms_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        from xscore.shared.terminal_ui import warn_line

        warn_line(f"Could not read transforms JSON: {e}")
        return None

    dpi = int(payload.get("dpi", 300))
    file_mid = float(payload.get("mid_y_pt", mid_y_pt))
    page_entries: list[dict] = payload.get("pages") or []
    if not page_entries:
        from xscore.shared.terminal_ui import warn_line

        warn_line("Transforms JSON has no pages — skip projected overlay")
        return None

    px_to_pt = 72.0 / dpi
    all_nodes = flatten_questions(questions)

    from xscore.shared.terminal_ui import warn_line

    use_tmp = output_pdf.resolve() == deskewed_pdf.resolve()
    save_path = output_pdf.with_suffix(".bbox_overlay_tmp.pdf") if use_tmp else output_pdf

    doc = fitz.open(str(deskewed_pdf))
    try:
        n_doc = len(doc)
        n_tf = len(page_entries)
        if n_tf != n_doc:
            warn_line(
                f"[bbox_overlay] transforms list has {n_tf} pages, PDF has {n_doc} "
                f"— overlaying min({n_tf}, {n_doc}) pages"
            )

        n_overlay = min(n_doc, n_tf)
        pages_data: list[dict] = []
        for page_idx in range(n_overlay):
            page = doc[page_idx]
            mid_px = int(round(page.rect.height / px_to_pt)) // 2

            pe = page_entries[page_idx]
            top_tf = similarity_transform_from_dict(pe["top"])
            bot_tf = similarity_transform_from_dict(pe["bot"])

            exercise: list[tuple[fitz.Rect, tuple[float, float, float]]] = []
            eq_blank: list[tuple[fitz.Rect, tuple[float, float, float]]] = []

            for color_idx, node in enumerate(all_nodes):
                for half, quad, color, is_eq in _projected_items_for_question_node(
                    node,
                    color_idx,
                    top_tf,
                    bot_tf,
                    scaffold_page=scaffold_page,
                    mid_y_pt=file_mid,
                ):
                    x0, y0, x1, y1 = quad
                    r = _half_page_px_to_page_rect(
                        x0, y0, x1, y1, half, mid_px, px_to_pt
                    )
                    r = r.intersect(page.rect)
                    if r.is_empty:
                        continue
                    if is_eq:
                        eq_blank.append((r, color))
                    else:
                        exercise.append((r, color))

            yellow_rects = compute_yellow_rects_for_page(
                page, all_nodes, top_tf, bot_tf,
                px_to_pt=px_to_pt,
                scaffold_page=scaffold_page,
                mid_y_pt=file_mid,
            )
            yellow = [(yr, _YELLOW) for yr in yellow_rects]

            exercise, yellow = _trim_first_exercise_per_subpage(
                exercise, yellow, page.rect.width, page.rect.height
            )
            yellow_rects = [yr for yr, _ in yellow]

            for r, color in exercise + eq_blank + yellow:
                page.draw_rect(r, color=color, width=line_width)

            pages_data.append({
                "page_idx": page_idx,
                "exercise": [
                    {"rect": [r.x0, r.y0, r.x1, r.y1], "color": list(c)}
                    for r, c in exercise
                ],
                "eq_blank": [
                    {"rect": [r.x0, r.y0, r.x1, r.y1]}
                    for r, _ in eq_blank
                ],
                "yellow": [
                    {"rect": [r.x0, r.y0, r.x1, r.y1]}
                    for r in yellow_rects
                ],
            })

        doc.save(str(save_path), garbage=4, deflate=True)
    finally:
        doc.close()

    if use_tmp:
        save_path.replace(output_pdf)

    if boxes_json is not None:
        boxes_json.write_text(
            json.dumps({"dpi": dpi, "pages": pages_data}, indent=2),
            encoding="utf-8",
        )

    return output_pdf


# ---------------------------------------------------------------------------
# CLI / quick validation helper
# ---------------------------------------------------------------------------

def _print_page_transforms(
    raw_4up_pdf: Path,
    reflines_json: Path,
    page_number: int = 1,
) -> None:
    """Print transforms and projected bboxes for *page_number* (1-based)."""
    raw_anchors = extract_raw_igcse_anchors(raw_4up_pdf)
    data = json.loads(Path(reflines_json).read_text())

    entry = next((e for e in data if e["page"] == page_number), None)
    if entry is None:
        from rich.panel import Panel

        from xscore.shared.terminal_ui import get_console

        get_console().print(
            Panel(
                f"Page {page_number} not found in alignment data.",
                border_style="red",
            )
        )
        return

    top_tf, bot_tf = compute_page_transforms(raw_anchors, entry["anchors"])
    from rich import box
    from rich.panel import Panel
    from rich.table import Table

    from xscore.shared.terminal_ui import get_console

    t = Table(
        box=box.ROUNDED,
        title=f"Page {page_number} transforms",
        title_style="bold cyan",
        show_header=False,
    )
    t.add_column("Field", style="dim")
    t.add_column("Value", overflow="fold")
    t.add_row("top_transform", str(top_tf))
    t.add_row("bot_transform", str(bot_tf))
    t.add_row("scale ratio top/bot", f"{top_tf.scale / bot_tf.scale:.5f}")
    get_console().print(Panel(t, border_style="dim cyan"))
