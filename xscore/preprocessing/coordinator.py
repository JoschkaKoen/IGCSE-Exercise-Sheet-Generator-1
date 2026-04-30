"""Clean a class scan PDF (rotate + de-blank + optional deskew) into *artifact_dir*."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

_NUMBERED_RE = re.compile(r"^scan[\s_\-]*(\d+)\b", re.IGNORECASE)

# Step folder names for scan preprocessing (steps 4–7)
_STEP_04 = "04_merge_duplex_scans"
_STEP_05 = "05_detect_blank_pages"
_STEP_06 = "06_autorotate"
_STEP_07 = "07_deskew"

# File name constants (no longer include the step-number prefix)
MERGED_SCAN_PDF           = _STEP_04 + "/merged_scan.pdf"
SCAN_ORIENTATIONS_JSON    = _STEP_04 + "/scan_orientations.json"
ORIENTED_SCAN_PDF         = _STEP_04 + "/oriented_scan.pdf"  # single-PDF flow only
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


def find_scan_pairs(folder: Path, artifact_dir: Path) -> list[tuple[Path, Path]] | None:
    """Return [(front, back), ...] ordered by index, or None if no numbered scans.

    A scan PDF is a numbered duplex member iff its filename matches
    ``^scan[\\s_-]*(\\d+)\\b`` (case-insensitive) and contains neither ``"dpi"``
    nor ``"cleaned"``. Odd indices are fronts, even indices are backs scanned
    after the stack was flipped; consecutive odd/even form one duplex pair.

    Raises ValueError on validation failure (gap, odd count, duplicate index,
    indices not starting at 1) — never silently drops files.
    """
    merged_out = artifact_dir / MERGED_SCAN_PDF
    indexed: dict[int, Path] = {}
    for f in folder.glob("*.pdf"):
        name_lower = f.name.lower()
        if "dpi" in name_lower or "cleaned" in name_lower:
            continue
        if f.resolve() == merged_out.resolve():
            continue
        m = _NUMBERED_RE.match(f.name)
        if not m:
            continue
        idx = int(m.group(1))
        if idx in indexed:
            raise ValueError(
                f"Duplicate scan index {idx}: {indexed[idx].name} and {f.name} "
                f"both extract index {idx}. Rename one so each pair index is unique."
            )
        indexed[idx] = f

    if not indexed:
        return None

    found = sorted(indexed.keys())
    expected = list(range(1, len(found) + 1))
    if found != expected:
        listing = ", ".join(p.name for _, p in sorted(indexed.items()))
        raise ValueError(
            f"Numbered scans must form a contiguous sequence 1..N; got indices {found} "
            f"from files [{listing}]. Expected {expected}."
        )
    if len(found) % 2:
        listing = ", ".join(p.name for _, p in sorted(indexed.items()))
        raise ValueError(
            f"Numbered scans must come in front+back pairs (even count); got {len(found)} "
            f"files [{listing}]. Each odd index needs a matching even successor."
        )

    return [(indexed[i], indexed[i + 1]) for i in range(1, len(found), 2)]


def prepare_scans_phase(
    folder: Path,
    artifact_dir: Path,
    dpi: int,
    *,
    force_rebuild: bool = False,
) -> Path:
    """Step 4 body. Always runs. Detects per-file orientation via Qwen
    vision (see :mod:`xscore.preprocessing.scan_orientation`), then either

    (a) merges duplex pairs into ``merged_scan.pdf`` with rotation applied, or
    (b) for a single source PDF, writes a rotated copy to ``oriented_scan.pdf``
        when rotation ≠ 0; otherwise returns the source path unchanged.

    Returns the path subsequent steps should read.

    *dpi* is plumbed through to :func:`find_source_scan_match` (used to
    prefer a DPI-tagged scan filename when multiple PDFs are present).
    """
    pairs = find_scan_pairs(folder, artifact_dir)
    if pairs is not None:
        return _prepare_duplex(pairs, artifact_dir, force_rebuild=force_rebuild)
    src = find_source_scan_match(folder, artifact_dir, dpi)
    return _prepare_single(src, artifact_dir, force_rebuild=force_rebuild)


def _prepare_duplex(
    pairs: list[tuple[Path, Path]],
    artifact_dir: Path,
    *,
    force_rebuild: bool = False,
) -> Path:
    """Per-file orientation detection + duplex interleave merge.

    Within each pair, the front PDF's pages are in order [p1, p3, p5, ...] and
    the back PDF's pages are in reverse order [p2N, p2N-2, ..., p2] (the stack
    was flipped before the back pass). They interleave to [p1, p2, p3, p4, ...].

    With multiple pairs, each pair is interleaved on its own and the resulting
    runs are concatenated in pair order.

    Cache validity requires *both* ``merged_scan.pdf`` AND
    ``scan_orientations.json`` to exist — pre-fix runs lacking the audit
    regenerate cleanly.
    """
    import fitz
    from xscore.shared.terminal_ui import info_line, ok_line, warn_line

    out = artifact_dir / MERGED_SCAN_PDF
    audit = artifact_dir / SCAN_ORIENTATIONS_JSON
    if out.is_file() and audit.is_file() and not force_rebuild:
        ok_line(f"Using cached {out.name}")
        try:
            cached = _read_orientations_audit(audit)
            _log_cached_orientation_summary(cached)
        except Exception as exc:  # noqa: BLE001 — replay is best-effort
            warn_line(f"scan_orientations.json could not be read: {exc!r}")
        return out

    from xscore.preprocessing.scan_orientation import (
        ROTATION_DETECTION_DPI,
        detect_scan_orientations,
    )
    from xscore.config import (
        SCAN_ORIENTATION_MODEL,
        SCAN_ORIENTATION_SAMPLE_PAGES,
    )
    from eXercise.ai_client import parse_model_spec

    unique_files = sorted({p for pair in pairs for p in pair}, key=lambda p: p.name)
    model_name, _, _ = parse_model_spec(SCAN_ORIENTATION_MODEL)
    info_line("Detecting per-file orientation")
    info_line(
        f"Model: {model_name} · "
        f"{max(1, SCAN_ORIENTATION_SAMPLE_PAGES)} sample pages per file at "
        f"{ROTATION_DETECTION_DPI} DPI"
    )
    results = detect_scan_orientations(unique_files)
    audit.parent.mkdir(parents=True, exist_ok=True)
    _write_orientations_audit(audit, results)

    pairs_desc = ", ".join(f"{f.stem}+{b.stem}" for f, b in pairs)
    info_line(
        f"Merging {len(unique_files)} files "
        f"({len(pairs)} duplex pair{'' if len(pairs) == 1 else 's'})  ·  "
        f"{pairs_desc}"
    )

    merged = fitz.open()
    try:
        total_pages = 0
        for front, back in pairs:
            front_rot = results[front].rotation_cw if front in results else 0
            back_rot  = results[back].rotation_cw  if back  in results else 0
            with fitz.open(str(front)) as doc_f, fitz.open(str(back)) as doc_b:
                if front_rot:
                    for p in doc_f:
                        p.set_rotation((p.rotation + front_rot) % 360)
                if back_rot:
                    for p in doc_b:
                        p.set_rotation((p.rotation + back_rot) % 360)
                nf, nb = len(doc_f), len(doc_b)
                if nf != nb:
                    warn_line(
                        f"Page count mismatch: {front.name}={nf}, {back.name}={nb}; "
                        f"pairing first {min(nf, nb)} from each"
                    )
                n = min(nf, nb)
                for i in range(n):
                    merged.insert_pdf(doc_f, from_page=i, to_page=i)
                    merged.insert_pdf(doc_b, from_page=nb - 1 - i, to_page=nb - 1 - i)
                total_pages += n * 2
        out.parent.mkdir(parents=True, exist_ok=True)
        merged.save(str(out))
    finally:
        merged.close()

    ok_line(f"{total_pages} pages merged into {out.name}")
    return out


def _prepare_single(
    src: Path,
    artifact_dir: Path,
    *,
    force_rebuild: bool = False,
) -> Path:
    """Detect orientation of a single source scan; if rotation is needed,
    write a rotated copy to ``ORIENTED_SCAN_PDF`` and return that. Otherwise
    return *src* unchanged (no copy written).

    Cache: when ``scan_orientations.json`` exists with rotation 0 for *src*,
    we trust it and skip the Qwen call. When it exists with rotation ≠ 0 but
    the rotated copy is missing, we regenerate.
    """
    import fitz
    from xscore.shared.terminal_ui import info_line, ok_line

    audit = artifact_dir / SCAN_ORIENTATIONS_JSON
    oriented = artifact_dir / ORIENTED_SCAN_PDF

    if audit.is_file() and not force_rebuild:
        try:
            cached = _read_orientations_audit(audit)
        except Exception:  # noqa: BLE001 — corrupt audit → regenerate
            cached = {}
        cached_rot = cached.get(src.name, None)
        if cached_rot is not None:
            if cached_rot == 0:
                ok_line(f"Using cached orientation for {src.name}")
                _log_cached_orientation_summary(cached)
                oriented.unlink(missing_ok=True)
                return src
            if oriented.is_file():
                ok_line(f"Using cached {oriented.name}")
                _log_cached_orientation_summary(cached)
                return oriented
            # rotation expected but file missing → fall through to regenerate

    from xscore.preprocessing.scan_orientation import (
        ROTATION_DETECTION_DPI,
        detect_scan_orientations,
    )
    from xscore.config import (
        SCAN_ORIENTATION_MODEL,
        SCAN_ORIENTATION_SAMPLE_PAGES,
    )
    from eXercise.ai_client import parse_model_spec

    model_name, _, _ = parse_model_spec(SCAN_ORIENTATION_MODEL)
    info_line("Detecting per-file orientation")
    info_line(
        f"Model: {model_name} · "
        f"{max(1, SCAN_ORIENTATION_SAMPLE_PAGES)} sample pages per file at "
        f"{ROTATION_DETECTION_DPI} DPI"
    )
    results = detect_scan_orientations([src])
    audit.parent.mkdir(parents=True, exist_ok=True)
    _write_orientations_audit(audit, results)

    rot = results[src].rotation_cw if src in results else 0
    if rot == 0:
        oriented.unlink(missing_ok=True)
        return src
    info_line(f"Writing rotated copy to {oriented.name}")
    with fitz.open(str(src)) as doc:
        for p in doc:
            p.set_rotation((p.rotation + rot) % 360)
        oriented.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(oriented))
    ok_line(f"Saved {oriented.name} (all pages rotated {rot}° CW)")
    return oriented


# ---------------------------------------------------------------------------
# Orientation audit JSON helpers (Step 4)
# ---------------------------------------------------------------------------

def _write_orientations_audit(path: Path, results: dict) -> None:
    """Serialize ``{Path: OrientationResult}`` to scan_orientations.json.

    Schema:
        {"schema_version": 1,
         "model": "<resolved-model-or-null>",
         "files": [{"name": "...", "rotation_cw": ..., "source": "qwen"|"fallback",
                    "reason": "..."?}, ...]}
    """
    import json
    files: list[dict] = []
    model: str | None = None
    for p, r in results.items():
        entry: dict = {
            "name": p.name,
            "rotation_cw": int(r.rotation_cw),
            "source": str(r.source),
        }
        if r.reason is not None:
            entry["reason"] = str(r.reason)
        files.append(entry)
        if model is None and r.model:
            model = r.model
    body = {
        "schema_version": 1,
        "model": model,
        "files": sorted(files, key=lambda e: e["name"]),
    }
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def _read_orientations_audit(path: Path) -> dict[str, int]:
    """Read scan_orientations.json and return ``{name: rotation_cw}``."""
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"unsupported scan_orientations.json schema: {path}")
    out: dict[str, int] = {}
    for entry in data.get("files", []) or []:
        out[str(entry["name"])] = int(entry.get("rotation_cw", 0))
    return out


def _log_cached_orientation_summary(cached: dict[str, int]) -> None:
    """Log a one-line-per-file replay from cached audit JSON.

    The audit JSON intentionally doesn't store per-page votes (kept compact),
    so this replay shows just the per-file decision.
    """
    from xscore.shared.terminal_ui import info_line
    if not cached:
        return
    info_line("Cached orientations:")
    for name in sorted(cached):
        rot = cached[name]
        if rot == 0:
            info_line(f"  {name}: already upright")
        else:
            info_line(f"  {name}: rotated {rot}° CW")


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
