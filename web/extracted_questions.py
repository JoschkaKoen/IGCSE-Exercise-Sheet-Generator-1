# -*- coding: utf-8 -*-
"""Read-only inspector helpers for the eXam bank's ``exam_questions.yaml``.

Powers the ``/learn/extracted/...`` verification routes — surfaces the
text/marks/numbering that the xscore scaffold extracted for each 2025
paper, so a reviewer can compare it against the source PDF.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from eXam.bank import BANK_ROOT, bank_dir_for
from eXercise.config import PAGE_HEADER_BY_EXAM
from xscore.shared.exam_questions_io import load_exam_questions_artifact
from xscore.shared.qnum_utils import norm_qnum


def _display_name(subject: str) -> str:
    return PAGE_HEADER_BY_EXAM.get(subject) or subject.replace("_", " ").title()


def _level(subject: str) -> str:
    if subject.startswith("a_level_"):
        return "a_level"
    if subject.startswith("igcse_"):
        return "igcse"
    return "other"


def list_subjects() -> list[dict]:
    """Subjects with at least one indexed paper in the bank.

    Each entry is ``{slug, display, level}``. Sorted by display name
    within each level so the landing groups read alphabetically.
    """
    if not BANK_ROOT.exists():
        return []
    out: list[dict] = []
    for entry in BANK_ROOT.iterdir():
        if not entry.is_dir():
            continue
        if not list_papers(entry.name):
            continue
        out.append(
            {"slug": entry.name, "display": _display_name(entry.name), "level": _level(entry.name)}
        )
    out.sort(key=lambda s: (s["level"], s["display"].lower()))
    return out


def list_papers(subject: str) -> list[str]:
    """Paper-stem directory names under the subject, sorted.

    Filters to entries that contain an ``exam_questions.yaml`` — the
    bank also hosts xscore artifact dirs (``06_detect_exam_layout/``
    etc.) at the same level, which must not show up as papers.
    """
    subject_dir = BANK_ROOT / subject
    if not subject_dir.is_dir():
        return []
    papers: list[str] = []
    for entry in subject_dir.iterdir():
        if not entry.is_dir():
            continue
        if not (entry / "exam_questions.yaml").exists():
            continue
        papers.append(entry.name)
    papers.sort()
    return papers


def load_paper(subject: str, paper_stem: str) -> dict | None:
    """Parsed ``exam_questions.yaml`` for the paper, or ``None`` if missing."""
    paper_dir = bank_dir_for(subject, Path(paper_stem))
    yaml_path = paper_dir / "exam_questions.yaml"
    if not yaml_path.exists():
        return None
    return load_exam_questions_artifact(yaml_path)


def load_subtopic_matches(subject: str, paper_stem: str) -> dict[str, list[str]] | None:
    """Read ``subtopic_matches.yaml`` produced by ``web.subtopic_matcher``.

    Returns the per-question ``{norm_qnum → [codes]}`` mapping, or ``None``
    when the sidecar is absent / unreadable.
    """
    paper_dir = bank_dir_for(subject, Path(paper_stem))
    path = paper_dir / "subtopic_matches.yaml"
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    raw = data.get("matches") or {}
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            out[str(k)] = [str(x) for x in v if x]
    return out


def attach_matches(questions: list[dict], matches: dict[str, list[str]]) -> None:
    """Set ``q["matched_subtopics"]`` on every node (parents + subquestions).

    Looks up each node's ``norm_qnum``-normalised number in *matches*; nodes
    without an entry get an empty list so the template's ``(q.matched_subtopics
    or [])`` always works.
    """
    def visit(qs: list[dict]) -> None:
        for q in qs:
            key = norm_qnum(str(q.get("number") or ""))
            q["matched_subtopics"] = matches.get(key, [])
            subs = q.get("subquestions") or []
            if subs:
                visit(subs)

    visit(questions)
