"""Runtime helpers for the eXam web layer.

Reads bank YAMLs to look up question metadata by ``question_id``. Computes
per-student question order for ``randomize=True`` tests. Reads attempts for
the resume / overview / take page logic.
"""

from __future__ import annotations

import functools
import json
import random
import re
from pathlib import Path

import fitz
import yaml

from eXam.bank import BANK_ROOT, bank_dir_for
from eXam.db import connect

_HUMAN_PAPER_RE = re.compile(
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{4}).*?Question Paper\s+(\d+)",
)
_CODE_PAPER_RE = re.compile(r"_([smw])(\d{2})_qp_(\d+)")
_CODE_SESSION = {"s": "June", "w": "November", "m": "March"}


def format_paper_label(paper_stem: str) -> str:
    """Render a paper stem as ``"<Session> <YYYY> · Paper <N>"`` for display.

    Handles the two filename conventions recognised by
    ``open_mode.list_practice_papers``: human-readable
    (``0625 Physics November 2025 Question Paper  11``) and Cambridge code
    (``0478_s25_qp_11``). Falls back to the raw stem if neither matches.
    """
    m = _HUMAN_PAPER_RE.search(paper_stem)
    if m:
        session, year, paper_num = m.groups()
        return f"{session} {year} · Paper {paper_num}"
    m = _CODE_PAPER_RE.search(paper_stem)
    if m:
        sess, yy, paper_num = m.groups()
        session = _CODE_SESSION.get(sess, sess)
        return f"{session} {2000 + int(yy)} · Paper {paper_num}"
    return paper_stem


@functools.lru_cache(maxsize=64)
def _load_paper_yaml(subject: str, paper_stem: str, name: str) -> dict:
    path = BANK_ROOT / subject / paper_stem / name
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def parse_question_id(question_id: str) -> tuple[str, str, str]:
    """``<subject>::<paper_stem>::<qnum>`` → (subject, paper_stem, qnum)."""
    parts = question_id.split("::")
    if len(parts) != 3:
        raise ValueError(f"bad question_id: {question_id!r}")
    return parts[0], parts[1], parts[2]


def question_metadata(question_id: str) -> dict | None:
    """Return ``{number, question_type, text, marks, options, has_images}`` or None."""
    subject, paper_stem, qnum = parse_question_id(question_id)
    data = _load_paper_yaml(subject, paper_stem, "exam_questions.yaml")
    for q in data.get("questions", []):
        if isinstance(q, dict) and str(q.get("number")) == qnum:
            opts = q.get("answer_options") or []
            pdf_w, pdf_h = _snippet_page_size(question_id)
            return {
                "question_id": question_id,
                "subject": subject,
                "paper_stem": paper_stem,
                "paper_label": format_paper_label(paper_stem),
                "number": qnum,
                "question_type": q.get("question_type"),
                "text": q.get("text", ""),
                "marks": q.get("marks", 1),
                "options": opts,
                "has_images": bool(q.get("images")),
                "pdf_width_pt": pdf_w,
                "pdf_height_pt": pdf_h,
            }
    return None


def _snippet_page_size(question_id: str) -> tuple[float | None, float | None]:
    """Return ``(width_pt, height_pt)`` of the snippet PDF's first page, or
    ``(None, None)`` if the file is missing or unreadable. Drives the
    iframe ``aspect-ratio`` on the practice page."""
    pdf_path = pdf_path_for(question_id)
    if not pdf_path.exists():
        return None, None
    try:
        with fitz.open(pdf_path) as doc:
            if len(doc) == 0:
                return None, None
            r = doc[0].rect
            return r.width, r.height
    except Exception:
        return None, None


def mark_scheme_entry(question_id: str) -> dict | None:
    subject, paper_stem, qnum = parse_question_id(question_id)
    data = _load_paper_yaml(subject, paper_stem, "mark_scheme.yaml")
    for q in data.get("questions", []):
        if isinstance(q, dict) and str(q.get("number")) == qnum:
            return q
    return None


def pdf_path_for(question_id: str) -> Path:
    subject, paper_stem, qnum = parse_question_id(question_id)
    return bank_dir_for(subject, Path(paper_stem)) / qnum / "question.pdf"


def question_order_for_student(test_id: str, student_id: int) -> list[str]:
    with connect() as conn:
        row = conn.execute(
            "SELECT question_ids, randomize FROM tests WHERE id=?", (test_id,)
        ).fetchone()
    if row is None:
        return []
    qids = json.loads(row["question_ids"])
    if not row["randomize"]:
        return qids
    seed = hash(f"{test_id}::{student_id}")
    rnd = random.Random(seed)
    qids = list(qids)
    rnd.shuffle(qids)
    return qids


def latest_attempts(test_id: str, student_id: int) -> dict[str, dict]:
    """Return ``{question_id: latest_attempt_row_as_dict}`` for this student+test."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT question_id, attempt_number, submitted, assigned_marks, max_marks,
                   reasoning, hint_used, solution_revealed, example_used, kb_used,
                   submitted_at
            FROM attempts
            WHERE student_id=? AND test_id=?
            ORDER BY question_id, attempt_number DESC
            """,
            (student_id, test_id),
        ).fetchall()
    out: dict[str, dict] = {}
    counts: dict[str, int] = {}
    for r in rows:
        d = dict(r)
        qid = d["question_id"]
        counts[qid] = counts.get(qid, 0) + 1
        if qid not in out:
            out[qid] = d
    for qid, d in out.items():
        d["attempt_count"] = counts[qid]
    return out
