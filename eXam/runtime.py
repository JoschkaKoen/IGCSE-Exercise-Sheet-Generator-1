"""Runtime helpers for the eXam web layer.

Reads bank YAMLs to look up question metadata by ``question_id``. Computes
per-student question order for ``randomize=True`` tests. Reads attempts for
the resume / overview / take page logic.
"""

from __future__ import annotations

import functools
import json
import random
from pathlib import Path

import yaml

from eXam.bank import BANK_ROOT, bank_dir_for
from eXam.db import connect


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
            return {
                "question_id": question_id,
                "subject": subject,
                "paper_stem": paper_stem,
                "number": qnum,
                "question_type": q.get("question_type"),
                "text": q.get("text", ""),
                "marks": q.get("marks", 1),
                "options": opts,
                "has_images": bool(q.get("images")),
            }
    return None


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
