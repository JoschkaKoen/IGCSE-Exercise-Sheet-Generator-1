"""Clean a class scan PDF (rotate + de-blank + optional deskew) into *artifact_dir*."""

from __future__ import annotations

import shutil
from pathlib import Path

# Phased pipeline artifacts (steps 4–7 scan block).
MERGED_SCAN_PDF           = "4_merged_scan.pdf"
SCAN_BLANKS_JSON          = "5_scan_blanks.json"
SCAN_ROTATED_PDF          = "6_scan_rotated.pdf"
CLEANED_SCAN_PDF          = "7_cleaned_scan.pdf"
SCAN_ANCHORS_JSON         = "7_scan_anchors.json"
SCAN_TRANSFORMS_JSON      = "7_scan_transforms.json"
SCAN_LINES_REMOVED_PDF    = "7_scan_lines_removed.pdf"
SCAN_BOXES_PROJECTED_PDF  = "7_scan_boxes_projected.pdf"
SCAN_BOXES_PROJECTED_JSON = "7_scan_boxes_projected.json"
SCAN_BOXES_REFINED_PDF    = "7_scan_boxes_refined.pdf"
SCAN_HANDWRITING_JSON     = "7_scan_handwriting.json"
SCAN_EXERCISE_BOXES_JSON  = "7_scan_exercise_boxes.json"
SCAN_EXERCISE_BOXES_PDF   = "7_scan_exercise_boxes.pdf"


def _scan_phase_paths(artifact_dir: Path) -> dict[str, Path]:
    out = artifact_dir / CLEANED_SCAN_PDF  # stem still used for transient paths only
    return {
        "merged":                 artifact_dir / MERGED_SCAN_PDF,
        "blanks_json":            artifact_dir / SCAN_BLANKS_JSON,
        "rotated":                artifact_dir / SCAN_ROTATED_PDF,
        "cleaned":                artifact_dir / CLEANED_SCAN_PDF,
        "sidecar":                artifact_dir / SCAN_ANCHORS_JSON,
        "sidecar_legacy":         out.with_name(f"{out.stem}_reflines.json"),  # transient
        "deskew_tmp":             artifact_dir / f"{out.stem}_deskew_tmp{out.suffix}",   # transient
        "transforms":             artifact_dir / SCAN_TRANSFORMS_JSON,
        "vlines_removed":         artifact_dir / SCAN_LINES_REMOVED_PDF,
        "projected":              artifact_dir / SCAN_BOXES_PROJECTED_PDF,
        "projected_boxes_json":   artifact_dir / SCAN_BOXES_PROJECTED_JSON,
        "refined":                artifact_dir / SCAN_BOXES_REFINED_PDF,
        "hw_results":             artifact_dir / SCAN_HANDWRITING_JSON,
        "adjusted_exercise_json": artifact_dir / SCAN_EXERCISE_BOXES_JSON,
        "adjusted_exercise_pdf":  artifact_dir / SCAN_EXERCISE_BOXES_PDF,
    }


def _remove_scan_pipeline_outputs(artifact_dir: Path, *, include_projected: bool = True) -> None:
    """Delete intermediate and final scan outputs under *artifact_dir* (force-clean)."""
    p = _scan_phase_paths(artifact_dir)
    for key, path in p.items():
        if key == "projected" and not include_projected:
            continue
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass


def find_source_scan_match(
    folder: Path,
    artifact_dir: Path,
    dpi: int,
) -> Path:
    """Pick the class-scan PDF under *folder* (same rules as :func:`cleanup_pdf`)."""
    output = artifact_dir / CLEANED_SCAN_PDF
    legacy_out = folder / CLEANED_SCAN_PDF
    scans = [
        f
        for f in folder.glob("*.pdf")
        if "scan" in f.name.lower()
        and f.resolve() not in {output.resolve(), legacy_out.resolve()}
    ]
    if not scans:
        raise FileNotFoundError(f"No scan PDF found in {folder}")
    return next(
        (s for s in scans if str(dpi) in s.stem),
        sorted(scans, key=lambda p: p.name.lower())[0],
    )


def find_two_scan_pdfs(folder: Path, artifact_dir: Path) -> tuple[Path, Path] | None:
    """Return (scan1, scan2) sorted by mtime if exactly 2 scan PDFs exist, else None.

    scan1 is the older file (fronts, scanned first).
    scan2 is the newer file (backs, scanned after flipping the stack).
    """
    merged_out = artifact_dir / MERGED_SCAN_PDF
    scans = [
        f for f in folder.glob("*.pdf")
        if "scan" in f.name.lower()
        and "cleaned" not in f.name.lower()
        and f.resolve() != merged_out.resolve()
    ]
    if len(scans) != 2:
        return None
    scans.sort(key=lambda p: p.stat().st_mtime)
    return scans[0], scans[1]


def merge_duplex_scans_phase(
    scan1: Path,
    scan2: Path,
    artifact_dir: Path,
    *,
    force_rebuild: bool = False,
) -> Path:
    """Step 4: interleave front and back single-sided scans into one double-sided PDF.

    scan1 pages are fronts in order [p1, p3, p5, ...].
    scan2 pages are backs in reverse order [p2N, p2N-2, ..., p2] (stack was flipped).
    Output interleaves them: [p1, p2, p3, p4, ...].
    """
    import fitz
    from xscore.shared.terminal_ui import ok_line, warn_line

    out = artifact_dir / MERGED_SCAN_PDF
    if out.is_file() and not force_rebuild:
        ok_line(f"Using cached {out.name}")
        return out

    doc1 = fitz.open(str(scan1))
    doc2 = fitz.open(str(scan2))
    n1, n2 = len(doc1), len(doc2)
    if n1 != n2:
        warn_line(
            f"Page count mismatch: {scan1.name}={n1}, {scan2.name}={n2}; "
            f"pairing first {min(n1, n2)} from each"
        )
    n = min(n1, n2)

    merged = fitz.open()
    for i in range(n):
        merged.insert_pdf(doc1, from_page=i, to_page=i)
        merged.insert_pdf(doc2, from_page=n2 - 1 - i, to_page=n2 - 1 - i)

    out.parent.mkdir(parents=True, exist_ok=True)
    merged.save(str(out))
    merged.close()
    doc1.close()
    doc2.close()
    ok_line(f"{n * 2} pages merged  ·  {scan1.name} + {scan2.name}")
    return out


def _scan_blanks_to_md(
    source_pdf: Path,
    total_pages: int,
    content_page_nums: list,
    blank_page_nums: list,
    page_render_sizes: list,
    blank_mean: float,
    blank_std: float,
    use_tesseract_rotation: bool,
    analysis_dpi: int,
) -> str:
    """Render a compact human-readable summary of scan blank-detection results."""
    from collections import Counter
    size_counts = Counter(f"{w}\u00d7{h}" for w, h in page_render_sizes)
    size_summary = ", ".join(f"{s} ({n} pages)" for s, n in size_counts.most_common())
    lines = [
        "# Scan Blanks Analysis",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| Source PDF | {source_pdf.name} |",
        f"| Total pages | {total_pages} |",
        f"| Content pages | {len(content_page_nums)} |",
        f"| Blank pages | {len(blank_page_nums)} |",
        f"| Analysis DPI | {analysis_dpi} |",
        f"| Blank detection | mean \u2265 {blank_mean}, std \u2264 {blank_std} |",
        f"| Tesseract rotation | {'Yes' if use_tesseract_rotation else 'No'} |",
        f"| Page sizes | {size_summary} |",
    ]
    return "\n".join(lines) + "\n"


def detect_blank_pages_phase(
    source_pdf: Path,
    artifact_dir: Path,
    *,
    analysis_dpi: int,
    force_clean_scan: bool = False,
    blank_mean: float | None = None,
    blank_std: float | None = None,
) -> Path:
    """Step 5: write ``scan_blanks.json`` with blank/content lists and render sizes."""
    from xscore.config import SCAN_USE_TESSERACT_ROTATION
    from xscore.preprocessing.remove_blanks_autorotate import (
        BLANK_MEAN_THRESHOLD,
        BLANK_STD_THRESHOLD,
        detect_blank_page_lists,
        scan_blanks_state_to_json,
    )
    from xscore.shared.terminal_ui import ok_line

    paths = _scan_phase_paths(artifact_dir)
    if force_clean_scan:
        _remove_scan_pipeline_outputs(artifact_dir)

    bm = blank_mean if blank_mean is not None else BLANK_MEAN_THRESHOLD
    bs = blank_std if blank_std is not None else BLANK_STD_THRESHOLD

    total_pages, content_page_nums, blank_page_nums, page_render_sizes = (
        detect_blank_page_lists(source_pdf, blank_mean=bm, blank_std=bs)
    )
    if not content_page_nums:
        raise RuntimeError("All scan pages classified as blank — nothing to process.")

    body = scan_blanks_state_to_json(
        source_pdf=source_pdf,
        total_pages=total_pages,
        content_page_nums=content_page_nums,
        blank_page_nums=blank_page_nums,
        page_render_sizes=page_render_sizes,
        blank_mean=bm,
        blank_std=bs,
        use_tesseract_rotation=SCAN_USE_TESSERACT_ROTATION,
        analysis_dpi=analysis_dpi,
    )
    paths["blanks_json"].parent.mkdir(parents=True, exist_ok=True)
    paths["blanks_json"].write_text(body, encoding="utf-8")
    paths["blanks_json"].with_suffix(".md").write_text(
        _scan_blanks_to_md(
            source_pdf=source_pdf,
            total_pages=total_pages,
            content_page_nums=content_page_nums,
            blank_page_nums=blank_page_nums,
            page_render_sizes=page_render_sizes,
            blank_mean=bm,
            blank_std=bs,
            use_tesseract_rotation=SCAN_USE_TESSERACT_ROTATION,
            analysis_dpi=analysis_dpi,
        ),
        encoding="utf-8",
    )
    ok_line(
        f"{len(content_page_nums)} content pages · {len(blank_page_nums)} blank"
    )
    return paths["blanks_json"]


def autorotate_phase(
    artifact_dir: Path,
    *,
    output_pdf: Path | None = None,
) -> Path:
    """Step 6: read ``scan_blanks.json``, write rotated PDF (blanks dropped)."""
    from xscore.preprocessing.remove_blanks_autorotate import (
        scan_blanks_state_from_json,
        write_rotated_pdf_after_blanks,
    )

    paths = _scan_phase_paths(artifact_dir)
    blanks_path = paths["blanks_json"]
    if not blanks_path.is_file():
        raise FileNotFoundError(f"Missing {blanks_path.name} — run blank detection first.")
    state = scan_blanks_state_from_json(blanks_path.read_text(encoding="utf-8"))
    source = Path(state["source_pdf"])
    if not source.is_file():
        raise FileNotFoundError(f"Source scan missing: {source}")

    out = output_pdf if output_pdf is not None else paths["rotated"]
    write_rotated_pdf_after_blanks(
        source,
        out,
        total_pages=int(state["total_pages"]),
        content_page_nums=list(state["content_page_nums"]),
        blank_page_nums=list(state["blank_page_nums"]),
        page_render_sizes=state["page_render_sizes"],
        analysis_dpi=int(state["analysis_dpi"]),
        use_tesseract_rotation=bool(state["use_tesseract_rotation"]),
    )
    return out


def deskew_phase(
    folder: Path,
    artifact_dir: Path,
    dpi: int,
    *,
    input_pdf: Path | None = None,
) -> Path:
    """Step 7: deskew ``scan_rotated.pdf`` (or *input_pdf*) into ``cleaned_scan.pdf``."""
    from xscore.preprocessing.deskew import deskew_pdf_raster

    paths = _scan_phase_paths(artifact_dir)
    inp = input_pdf if input_pdf is not None else paths["rotated"]
    if not inp.is_file():
        raise FileNotFoundError(f"Missing rotated scan: {inp}")

    out = paths["cleaned"]
    tmp_deskew = paths["deskew_tmp"]
    deskew_pdf_raster(
        input_pdf=inp,
        output_pdf=tmp_deskew,
        dpi=dpi,
        saved_as=out.name,
    )
    shutil.move(str(tmp_deskew), str(out))
    return out


def detect_page_anchors_phase(
    folder: Path,
    artifact_dir: Path,
    dpi: int,
) -> None:
    """Step 8: fill IGCSE header anchors in the deskew sidecar."""
    del folder  # reserved for API symmetry with other phases
    from xscore.preprocessing.deskew import detect_page_anchors_for_cleaned_scan

    paths = _scan_phase_paths(artifact_dir)
    deskewed = paths["cleaned"]
    sidecar = paths["sidecar"]
    if not deskewed.is_file():
        raise FileNotFoundError(f"Missing {deskewed.name} — run deskew first.")
    if not sidecar.is_file():
        raise FileNotFoundError(f"Missing {sidecar.name} — run deskew first.")
    detect_page_anchors_for_cleaned_scan(deskewed, sidecar, dpi)


def compute_transformation_phase(
    folder: Path,
    artifact_dir: Path,
    dpi: int,
    *,
    force_layout_mismatch: bool = False,
) -> Path | None:
    """Step 9: write ``cleaned_scan_transforms.json`` (4-up ↔ scan similarity per page)."""
    from xscore.preprocessing.deskew import resolve_deskew_sidecar
    from scaffold.project_boxes_on_scanned_exam import (
        find_raw_four_up_pdf,
        write_scan_page_transforms_json,
    )
    from scaffold.generate_scaffold import find_exam_pdf
    from xscore.shared.terminal_ui import info_line, ok_line

    paths = _scan_phase_paths(artifact_dir)
    deskewed = paths["cleaned"]
    transforms_path = paths["transforms"]
    if not deskewed.is_file():
        raise FileNotFoundError(f"Missing {deskewed.name} — run deskew first.")

    folder = Path(folder)
    raw4 = find_raw_four_up_pdf(folder)
    if raw4 is None:
        msg = "No *4up* raw exam PDF — skip transforms JSON"
        info_line(msg)
        if transforms_path.is_file():
            transforms_path.unlink()
        return None

    try:
        exam_for_scaffold = find_exam_pdf(folder)
    except FileNotFoundError:
        info_line("No raw exam PDF — skip transforms JSON")
        if transforms_path.is_file():
            transforms_path.unlink()
        return None

    if not force_layout_mismatch and exam_for_scaffold.resolve() != raw4.resolve():
        msg = (
            "Skip transforms JSON: exam PDF used for scaffold does not match the four-up file."
        )
        info_line(msg)
        if transforms_path.is_file():
            transforms_path.unlink()
        return None

    sidecar = resolve_deskew_sidecar(deskewed)
    if sidecar is None or not sidecar.is_file():
        info_line("Missing anchor sidecar — skip transforms JSON")
        if transforms_path.is_file():
            transforms_path.unlink()
        return None

    if write_scan_page_transforms_json(
        raw4,
        sidecar,
        transforms_path,
        dpi=dpi,
    ):
        ok_line("Done")
        return transforms_path
    if transforms_path.is_file():
        transforms_path.unlink()
    return None


def project_bounding_boxes_phase(
    folder: Path,
    artifact_dir: Path,
    dpi: int,
    *,
    force_layout_mismatch: bool = False,
) -> Path | None:
    """Step 10: draw ``*_projected_boxes.pdf`` using transforms from step 9."""
    from xscore.preprocessing.deskew import resolve_deskew_sidecar
    from scaffold.generate_scaffold import find_exam_pdf, build_scaffold
    from scaffold.project_boxes_on_scanned_exam import (
        find_raw_four_up_pdf,
        overlay_projected_scaffold_from_transforms_json,
        overlay_projected_scaffold_on_scan_pdf,
    )
    from xscore.shared.terminal_ui import info_line

    paths = _scan_phase_paths(artifact_dir)
    deskewed = paths["cleaned"]
    transforms_path = paths["transforms"]
    projected = paths["projected"]
    folder = Path(folder)
    ad = artifact_dir

    if not deskewed.is_file():
        raise FileNotFoundError(f"Missing {deskewed.name} — run deskew first.")

    raw4 = find_raw_four_up_pdf(folder)
    if raw4 is None:
        info_line("No *4up* raw exam PDF — skip projected scaffold PDF")
        return None
    try:
        exam_for_scaffold = find_exam_pdf(folder)
    except FileNotFoundError:
        info_line("No raw exam PDF — skip projected scaffold PDF")
        return None
    if not force_layout_mismatch and exam_for_scaffold.resolve() != raw4.resolve():
        info_line(
            "Skip projected PDF: scaffold exam PDF does not match the four-up file."
        )
        return None

    sidecar = resolve_deskew_sidecar(deskewed)
    if sidecar is None:
        info_line("Missing anchor sidecar — skip projected PDF")
        return None

    try:
        scaffold = build_scaffold(
            folder,
            artifact_dir=ad,
            quiet=True,
        )
        roots = scaffold.questions
    except Exception as e:  # noqa: BLE001
        from xscore.shared.terminal_ui import warn_line

        warn_line(f"Could not load scaffold for projected overlay: {e}")
        return None

    try:
        if transforms_path.is_file():
            out = overlay_projected_scaffold_from_transforms_json(
                deskewed,
                transforms_path,
                roots,
                projected,
                boxes_json=paths["projected_boxes_json"],
            )
            return out if out is not None and out.is_file() else None
        info_line(
            "No transforms file — drawing projected boxes using sidecar (legacy path)"
        )
        overlay_projected_scaffold_on_scan_pdf(
            deskewed,
            sidecar,
            raw4,
            roots,
            projected,
            dpi=dpi,
        )
        return projected if projected.is_file() else None
    except Exception as e:
        from xscore.shared.terminal_ui import warn_line

        warn_line(f"Projected scaffold overlay failed: {e}")
        return None
