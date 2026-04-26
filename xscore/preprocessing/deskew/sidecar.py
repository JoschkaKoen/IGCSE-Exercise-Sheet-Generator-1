"""Sidecar JSON path resolution + small format helpers."""

from __future__ import annotations

from pathlib import Path

from xscore.preprocessing.deskew.types import ReferenceLine


def _lines_str(lines: list[ReferenceLine]) -> str:
    """Compact one-line summary of up to 3 detected reference lines."""
    labels = ["L", "C", "R"]
    parts = []
    for label, ln in zip(labels, lines):
        parts.append(f"{label}({ln.x_center},{ln.y_start}..{ln.y_end})")
    return "  ".join(parts) if parts else "(none)"


def anchors_sidecar_path(deskewed_pdf: Path) -> Path:
    """Path for the IGCSE anchor sidecar next to a deskewed raster PDF."""
    from xscore.preprocessing.coordinator import SCAN_ANCHORS_JSON
    return Path(deskewed_pdf).parent / SCAN_ANCHORS_JSON


def resolve_deskew_sidecar(deskewed_pdf: Path) -> Path | None:
    """Return an existing anchor sidecar path, or *None*.

    Prefers ``<stem>_anchors.json``; falls back to legacy ``<stem>_reflines.json``.
    """
    p = Path(deskewed_pdf)
    newer = anchors_sidecar_path(p)
    if newer.is_file():
        return newer
    legacy = p.with_name(p.stem + "_reflines.json")
    if legacy.is_file():
        return legacy
    return None
