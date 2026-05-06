"""Merge + deskew a class scan PDF into a single PDF under *artifact_dir*."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from xscore.shared.step_folders import (
    CLEANED_SCAN_PDF,
    DESKEW_DIR,
    MERGE_DUPLEX_DIR,
)

_NUMBERED_RE = re.compile(r"^scan[\s_\-]*(\d+)\b", re.IGNORECASE)

# File name constants (no longer include the step-number prefix)
MERGED_SCAN_PDF           = MERGE_DUPLEX_DIR + "/merged_scan.pdf"
SCAN_ORIENTATIONS_JSON    = MERGE_DUPLEX_DIR + "/scan_orientations.json"
ORIENTED_SCAN_PDF         = MERGE_DUPLEX_DIR + "/oriented_scan.pdf"  # single-PDF flow only
DESKEW_ANGLES_JSON        = DESKEW_DIR + "/deskew_angles.json"


def _scan_phase_paths(artifact_dir: Path) -> dict[str, Path]:
    cleaned = artifact_dir / CLEANED_SCAN_PDF
    return {
        "merged":     artifact_dir / MERGED_SCAN_PDF,
        "cleaned":    cleaned,
        "deskew_tmp": cleaned.with_name(f"{cleaned.stem}_deskew_tmp{cleaned.suffix}"),
    }


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
    """prepare_scans body. Always runs. Detects per-file orientation via Qwen
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


def _append_blank_like(target_doc, source_doc) -> None:
    """Append a blank page to *target_doc* matching *source_doc*[0]'s
    dimensions and rotation. Falls back to A4 portrait when source is empty."""
    if len(source_doc) > 0:
        ref = source_doc[0]
        w, h, rot = ref.rect.width, ref.rect.height, ref.rotation
    else:
        w, h, rot = 595.0, 842.0, 0
    page = target_doc.new_page(width=w, height=h)
    if rot:
        page.set_rotation(rot)


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
    from xscore.shared.terminal_ui import blank_line, info_line, ok_line, warn_line

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

    from xscore.preprocessing.scan_orientation import detect_scan_orientations

    unique_files = sorted({p for pair in pairs for p in pair}, key=lambda p: p.name)
    _emit_orientation_phase_header(info_line)
    blank_line()
    results = detect_scan_orientations(unique_files)
    audit.parent.mkdir(parents=True, exist_ok=True)
    _write_orientations_audit(audit, results)

    pairs_desc = ", ".join(f"{f.stem}+{b.stem}" for f, b in pairs)
    blank_line()
    if len(pairs) == 1:
        info_line(f"Merging duplex pair: {pairs_desc}")
    else:
        info_line(f"Merging {len(pairs)} duplex pairs: {pairs_desc}")

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
                    import os as _os  # local import — avoid touching the public preamble
                    if _os.environ.get("DUPLEX_PAD_STRICT", "0") == "1":
                        warn_line(
                            f"DUPLEX_PAD_STRICT=1: aborting; page count mismatch "
                            f"{front.name}={nf}, {back.name}={nb}."
                        )
                        raise SystemExit(1)
                    warn_line(
                        f"Page count mismatch: {front.name}={nf}, {back.name}={nb}; "
                        f"padding {abs(nf - nb)} blank page(s) at end-of-merged-stack — "
                        f"note this may not be the position the page is actually missing "
                        f"from. Step 18 will flag any per-student page_set_anomaly that "
                        f"results (set DUPLEX_PAD_STRICT=1 to abort here instead)."
                    )
                n = max(nf, nb)
                for i in range(n):
                    if i < nf:
                        merged.insert_pdf(doc_f, from_page=i, to_page=i)
                    else:
                        _append_blank_like(merged, doc_f)
                    back_idx = nb - 1 - i
                    if 0 <= back_idx < nb:
                        merged.insert_pdf(doc_b, from_page=back_idx, to_page=back_idx)
                    else:
                        _append_blank_like(merged, doc_b)
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
    from xscore.shared.terminal_ui import blank_line, info_line, ok_line

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

    from xscore.preprocessing.scan_orientation import detect_scan_orientations

    _emit_orientation_phase_header(info_line)
    blank_line()
    results = detect_scan_orientations([src])
    audit.parent.mkdir(parents=True, exist_ok=True)
    _write_orientations_audit(audit, results)

    rot = results[src].rotation_cw if src in results else 0
    if rot == 0:
        oriented.unlink(missing_ok=True)
        return src
    blank_line()
    info_line(f"Writing rotated copy to {oriented.name}")
    with fitz.open(str(src)) as doc:
        for p in doc:
            p.set_rotation((p.rotation + rot) % 360)
        oriented.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(oriented))
    ok_line(f"Saved {oriented.name} (all pages rotated {rot}° CW)")
    return oriented


# ---------------------------------------------------------------------------
# Orientation phase header (prepare_scans)
# ---------------------------------------------------------------------------

def _emit_orientation_phase_header(info_line) -> None:
    """Emit the Step-4 'Detecting per-file orientation' header lines.

    Adapts the second info_line to the configured detector:
    - tesseract: "Targeting N usable votes per file at 150 DPI · escalating with M more if not unanimous"
    - ai:        "Model: <name> · N initial + up to M escalation pages per file at 300 DPI"
    - auto:      "Auto (Tesseract preferred, AI fallback)"
    """
    from xscore.config import (  # noqa: PLC0415
        SCAN_ORIENTATION_DETECTOR,
        SCAN_ORIENTATION_INITIAL_VOTES,
        SCAN_ORIENTATION_ESCALATION_VOTES,
        SCAN_ORIENTATION_MODEL,
    )
    from xscore.preprocessing.scan_orientation import (  # noqa: PLC0415
        ROTATION_DETECTION_DPI,
        TESS_OSD_DPI,
        _resolve_detector,
    )
    from eXercise.ai_client import parse_model_spec  # noqa: PLC0415

    initial = max(1, SCAN_ORIENTATION_INITIAL_VOTES)
    escalation = max(0, SCAN_ORIENTATION_ESCALATION_VOTES)
    detector = _resolve_detector()  # final detector (after auto-resolution)

    if detector == "tesseract":
        if escalation == 0:
            tess_desc = (
                f"Detecting orientation · Tesseract OSD · "
                f"{initial} votes/file @ {TESS_OSD_DPI} DPI (no escalation)"
            )
        else:
            tess_desc = (
                f"Detecting orientation · Tesseract OSD · "
                f"{initial} votes/file @ {TESS_OSD_DPI} DPI · "
                f"+{escalation} on split"
            )
        info_line(tess_desc)
        if SCAN_ORIENTATION_DETECTOR == "auto":
            info_line("(auto: Tesseract available, using it)")
    else:  # ai
        model_name, _, _ = parse_model_spec(SCAN_ORIENTATION_MODEL)
        if escalation == 0:
            ai_desc = (
                f"Detecting orientation · AI vision · model {model_name} · "
                f"{initial} sample pages/file @ {ROTATION_DETECTION_DPI} DPI "
                "(no escalation)"
            )
        else:
            ai_desc = (
                f"Detecting orientation · AI vision · model {model_name} · "
                f"{initial} initial + up to {escalation} escalation pages/file "
                f"@ {ROTATION_DETECTION_DPI} DPI"
            )
        info_line(ai_desc)
        from xscore.preprocessing.scan_orientation import _JPEG_QUALITY  # noqa: PLC0415
        from xscore.shared.terminal_ui import announce_ai_input  # noqa: PLC0415
        announce_ai_input(kind="JPEG", dpi=ROTATION_DETECTION_DPI, quality=_JPEG_QUALITY)


# ---------------------------------------------------------------------------
# Orientation audit JSON helpers (prepare_scans)
# ---------------------------------------------------------------------------

def _write_orientations_audit(path: Path, results: dict) -> None:
    """Serialize ``{Path: OrientationResult}`` to scan_orientations.json.

    Schema:
        {"schema_version": 2,
         "detector": "tesseract" | "ai" | "fallback",
         "model": "<resolved-ai-model-or-null>",
         "files": [{"name": "...", "rotation_cw": ...,
                    "source": "model" | "fallback",
                    "detector": "tesseract" | "ai" | "fallback",
                    "reason": "..."?}, ...]}
    """
    import json
    files: list[dict] = []
    model: str | None = None
    top_detector: str | None = None
    for p, r in results.items():
        entry: dict = {
            "name": p.name,
            "rotation_cw": int(r.rotation_cw),
            "source": str(r.source),
            "detector": str(r.detector),
        }
        if r.reason is not None:
            entry["reason"] = str(r.reason)
        files.append(entry)
        if model is None and r.model:
            model = r.model
        if top_detector is None and r.source == "model":
            top_detector = r.detector
    body = {
        "schema_version": 2,
        "detector": top_detector,
        "model": model,
        "files": sorted(files, key=lambda e: e["name"]),
    }
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


def _read_orientations_audit(path: Path) -> dict[str, int]:
    """Read scan_orientations.json and return ``{name: rotation_cw}``.

    Accepts both schema_version 1 (pre-Tesseract-primary) and 2.
    """
    import json
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") not in (1, 2):
        raise ValueError(f"unsupported scan_orientations.json schema: {path}")
    out: dict[str, int] = {}
    for entry in data.get("files", []) or []:
        out[str(entry["name"])] = int(entry.get("rotation_cw", 0))
    return out


def _log_cached_orientation_summary(cached: dict[str, int]) -> None:
    """Log a one-line-per-file replay from cached audit JSON.

    The audit JSON intentionally doesn't store per-page votes (kept compact),
    so this replay shows just the per-file decision — mirroring the fresh-run
    decision-line shape, with a `(cached)` suffix.
    """
    from xscore.shared.terminal_ui import blank_line, ok_line
    if not cached:
        return
    blank_line()
    for i, name in enumerate(sorted(cached)):
        if i:
            blank_line()
        rot = cached[name]
        if rot == 0:
            ok_line(f"{name}: already upright (cached)")
        else:
            ok_line(f"{name}: applying rotation {rot}° CW (cached)")


def deskew_phase(
    artifact_dir: Path,
    dpi: int,
    *,
    input_pdf: Path | None = None,
) -> Path:
    """deskew body: deskew the merged/oriented scan into the final consolidated PDF.

    Reads the path returned by :func:`prepare_scans_phase` (passed via *input_pdf*),
    deskews each page, and writes the result to ``CLEANED_SCAN_PDF`` at the
    artifact-dir root. Step 4's transient ``merged_scan.pdf`` /
    ``oriented_scan.pdf`` are deleted on success so step 7 owns the only PDF
    saved by steps 1-7.
    """
    from xscore.preprocessing.deskew import deskew_pdf_raster

    paths = _scan_phase_paths(artifact_dir)
    inp = input_pdf if input_pdf is not None else paths["merged"]
    if not inp.is_file():
        raise FileNotFoundError(f"Missing prepared scan: {inp}")

    out = paths["cleaned"]
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_deskew = paths["deskew_tmp"]
    deskew_pdf_raster(
        input_pdf=inp,
        output_pdf=tmp_deskew,
        dpi=dpi,
        saved_as=out.name,
        angles_json_path=artifact_dir / DESKEW_ANGLES_JSON,
    )
    shutil.move(str(tmp_deskew), str(out))

    # Step 7 owns the only saved PDF in steps 1-7.
    # Note: _prepare_single's no-rotation branch returns the user's source path
    # (outside artifact_dir) — it's never one of the paths below, so this loop
    # cannot accidentally clobber the source PDF.
    for tmp in (artifact_dir / MERGED_SCAN_PDF, artifact_dir / ORIENTED_SCAN_PDF):
        tmp.unlink(missing_ok=True)
    return out
