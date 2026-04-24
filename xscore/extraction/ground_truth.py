"""Ground truth file loading, fuzzy name match, per-student accuracy."""

from __future__ import annotations

from difflib import SequenceMatcher


def fuzzy_match_name(extracted_name: str, gt_names: list[str]) -> str | None:
    """Find the best matching ground truth name using fuzzy matching."""
    if not extracted_name or extracted_name in ("UNKNOWN", "EXTRACTION_ERROR", "?"):
        return None

    extracted_lower = extracted_name.lower().strip()

    for gt_name in gt_names:
        if gt_name.lower() == extracted_lower:
            return gt_name

    for gt_name in gt_names:
        gt_lower = gt_name.lower()
        if extracted_lower in gt_lower or gt_lower in extracted_lower:
            return gt_name

    best_match = None
    best_ratio = 0.0
    for gt_name in gt_names:
        ratio = SequenceMatcher(None, extracted_lower, gt_name.lower()).ratio()
        if ratio > best_ratio and ratio >= 0.6:
            best_ratio = ratio
            best_match = gt_name

    return best_match


