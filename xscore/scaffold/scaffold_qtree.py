"""Recursive walks over the raw_questions tree + question-number normalization.

Pure stdlib — depends only on ``re``. Used by the scaffold helpers in
steps 19, 20, 22, 23, and 24.
"""

from __future__ import annotations

import re


def _norm_qnum(s: str) -> str:
    return re.sub(r"[()]", "", s)


def _format_qnums_for_line(qnums: list[str], limit: int = 10) -> str:
    """Comma-separated qnum list, truncated when long.

    For ``len > limit``: shows the first 8 followed by ``… (+N more)`` so the
    line stays bounded on terminals while still naming the leading questions.
    """
    if len(qnums) <= limit:
        return ", ".join(qnums)
    head = qnums[:8]
    return ", ".join(head) + f", … (+{len(qnums) - 8} more)"


def _collect_qnums(raw_questions: list[dict]) -> list[str]:
    """Walk *raw_questions* recursively, return ordered unique question numbers
    (top-level + nested subquestions). Preserves first-seen order."""
    seen: dict[str, None] = {}

    def visit(node: dict) -> None:
        n = str(node.get("number", "")).strip()
        if n and n not in seen:
            seen[n] = None
        for sub in (node.get("subquestions") or []):
            visit(sub)

    for q in raw_questions:
        visit(q)
    return list(seen.keys())


def _leaf_qnums(raw_questions: list[dict]) -> list[str]:
    """Return question numbers for leaves only (nodes with no subquestions).

    These are the questions we expect the mark scheme to actually contain
    content for — parents of subquestions ("2" with children "2a", "2b")
    typically have no own criteria and are deliberately left empty by the AI.
    Used to scope the "no content extracted" warning to actionable misses.
    """
    out: list[str] = []

    def visit(node: dict) -> None:
        subs = node.get("subquestions") or []
        if subs:
            for sub in subs:
                visit(sub)
        else:
            n = str(node.get("number", "")).strip()
            if n:
                out.append(n)

    for q in raw_questions:
        visit(q)
    return out


def _filter_questions_by_qnums(
    raw_questions: list[dict], allowed: set[str],
) -> list[dict]:
    """Walk *raw_questions* recursively, keep only nodes whose ``number`` is in
    *allowed*. Returns a flat list — ``build_scheme_scaffold`` flattens via
    ``_visit`` anyway, so flattening here is consistent. Subquestions are
    detached (the caller wants per-question entries, not a parent skeleton)."""
    out: list[dict] = []

    def visit(node: dict) -> None:
        if str(node.get("number", "")) in allowed:
            shallow = {k: v for k, v in node.items() if k != "subquestions"}
            shallow["subquestions"] = []
            out.append(shallow)
        for sub in (node.get("subquestions") or []):
            visit(sub)

    for q in raw_questions:
        visit(q)
    return out
