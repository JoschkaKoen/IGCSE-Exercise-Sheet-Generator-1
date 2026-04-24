"""Clean a class scan PDF (rotate + de-blank + optional deskew) into *artifact_dir*."""

from __future__ import annotations

import shutil
from pathlib import Path

# Step folder names for scan preprocessing (steps 4–7)
_STEP_04 = "04_merge_duplex_scans"
_STEP_05 = "05_detect_blank_pages"
_STEP_06 = "06_autorotate"
_STEP_07 = "07_deskew"

# File name constants (no longer include the step-number prefix)
MERGED_SCAN_PDF           = _STEP_04 + "/merged_scan.pdf"
SCAN_BLANKS_JSON          = _STEP_05 + "/scan_blanks.json"
SCAN_ROTATED_PDF          = _STEP_06 + "/scan_rotated.pdf"
CLEANED_SCAN_PDF          = _STEP_07 + "/cleaned_scan.pdf"
SCAN_ANCHORS_JSON         = _STEP_07 + "/scan_anchors.json"
SCAN_TRANSFORMS_JSON      = _STEP_07 + "/scan_transforms.json"
SCAN_LINES_REMOVED_PDF    = _STEP_07 + "/scan_lines_removed.pdf"
SCAN_BOXES_PROJECTED_PDF  = _STEP_07 + "/scan_boxes_projected.pdf"
SCAN_BOXES_PROJECTED_JSON = _STEP_07 + "/scan_boxes_projected.json"
SCAN_BOXES_REFINED_PDF    = _STEP_07 + "/scan_boxes_refined.pdf"
SCAN_HANDWRITING_JSON     = _STEP_07 + "/scan_handwriting.json"
SCAN_EXERCISE_BOXES_JSON  = _STEP_07 + "/scan_exercise_boxes.json"
SCAN_EXERCISE_BOXES_PDF   = _STEP_07 + "/scan_exercise_boxes.pdf"


def _scan_phase_paths(artifact_dir: Path) -> dict[str, Path]:
    out = artifact_dir / CLEANED_SCAN_PDF  # stem still used for transient paths only
    return {
        "merged":                 artifact_dir / MERGED_SCAN_PDF,
        "blanks_json":            artifact_dir / SCAN_BLANKS_JSON,
        "rotated":                artifact_dir / SCAN_ROTATED_PDF,
        "cleaned":                artifact_dir / CLEANED_SCAN_PDF,
        "sidecar":                artifact_dir / SCAN_ANCHORS_JSON,
        "sidecar_legacy":         out.with_name(f"{out.stem}_reflines.json"),  # transient
        "deskew_tmp":             out.with_name(f"{out.stem}_deskew_tmp{out.suffix}"),  # transient
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
    out.parent.mkdir(parents=True, exist_ok=True)
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
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_deskew = paths["deskew_tmp"]
    deskew_pdf_raster(
        input_pdf=inp,
        output_pdf=tmp_deskew,
        dpi=dpi,
        saved_as=out.name,
    )
    shutil.move(str(tmp_deskew), str(out))
    return out
