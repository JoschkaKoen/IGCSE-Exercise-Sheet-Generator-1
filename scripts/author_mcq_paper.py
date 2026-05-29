"""Author a Cambridge multiple-choice paper's bank YAMLs from the PDF text layer.

MCQ marking is deterministic on the answer letter, and the practice UI renders the
question from the snippet PDF (option text is never shown — radios are bare A/B/C/D),
so the only correctness-critical data is: the question count, four option letters
each, and the correct-answer letters from the mark scheme. Question stems are captured
best-effort from the text layer for helper-drawer quality (``meta["text"]``); they are
not displayed, so garbled math/figure stems are harmless.

Writes ``exam_questions.yaml`` + ``mark_scheme.yaml`` into the bank dir. Finalize with::

    python -m eXam.bank --authored --paper <qp> --ms <ms> --subject <slug>

which renders the per-question snippets and stamps the cache.

Usage::

    python -m scripts.author_mcq_paper --subject igcse_physics \
        --paper "exams/.../0625 ... Question Paper  21.pdf" \
        --ms    "exams/.../0625 ... Mark Scheme  21.pdf"
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import fitz
import yaml

from eXam.bank import bank_dir_for

OPT_LETTERS = ("A", "B", "C", "D")
# Page furniture / footers to drop from stems.
_NOISE = re.compile(
    r"©|UCLES|Cambridge|\[Turn over|BLANK PAGE|/O/N/|/M/J/|/F/M/|"
    r"^\d{1,2}$|^Page \d|^This document"
)


def _lines(pdf: Path) -> list[str]:
    doc = fitz.open(pdf)
    try:
        out: list[str] = []
        for page in doc:
            out.extend(page.get_text().splitlines())
        return out
    finally:
        doc.close()


def parse_answers(ms_pdf: Path) -> dict[int, str]:
    """Parse the 'Question / Answer / Marks' grid → ``{qnum: letter}``."""
    cl = [ln.strip() for ln in _lines(ms_pdf) if ln.strip()]
    answers: dict[int, str] = {}
    for i in range(len(cl) - 1):
        if re.fullmatch(r"\d{1,2}", cl[i]) and re.fullmatch(r"[A-D]", cl[i + 1]):
            answers[int(cl[i])] = cl[i + 1]
    return answers


def parse_stems(qp_pdf: Path, max_q: int) -> dict[int, str]:
    """Best-effort stem per question: text between the question-number marker and
    its first lone option letter. Sequentially anchored from the first standalone
    ``"1"`` so cover-page noise (``1.0 kg`` etc.) can't false-match."""
    lines = _lines(qp_pdf)
    n = len(lines)
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "1"), 0)
    markers: dict[int, int] = {}
    q = 1
    i = start
    while i < n and q <= max_q:
        if re.match(rf"^{q}\b", lines[i].strip()):
            markers[q] = i
            q += 1
        i += 1
    stems: dict[int, str] = {}
    ordered = sorted(markers)
    for j, qn in enumerate(ordered):
        mi = markers[qn]
        end = markers[ordered[j + 1]] if j + 1 < len(ordered) else n
        block = list(lines[mi:end])
        block[0] = re.sub(rf"^\s*{qn}\s*", "", block[0])
        kept: list[str] = []
        for ln in block:
            s = ln.strip()
            if s in OPT_LETTERS:  # reached the option list
                break
            if not s or _NOISE.search(s):
                continue
            kept.append(s)
        stems[qn] = " ".join(kept).strip()
    return stems


def build(subject: str, qp_pdf: Path, ms_pdf: Path) -> tuple[dict, dict, dict]:
    answers = parse_answers(ms_pdf)
    if not answers:
        raise SystemExit(f"no answer grid parsed from {ms_pdf.name}")
    qnums = sorted(answers)
    stems = parse_stems(qp_pdf, max(qnums))
    questions = []
    scheme = []
    for q in qnums:
        num = str(q)
        questions.append(
            {
                "number": num,
                "question_type": "multiple_choice",
                "marks": 1,
                "text": stems.get(q, ""),
                "answer_options": [{"letter": ltr, "text": ltr} for ltr in OPT_LETTERS],
                "subquestions": [],
            }
        )
        scheme.append(
            {
                "number": num,
                "question_type": "multiple_choice",
                "correct_answer": answers[q],
                "explanation": None,
                "mark_scheme_answer": None,
                "mark_scheme": [],
                "graphics": [],
            }
        )
    return {"questions": questions}, {"questions": scheme}, {
        "n": len(qnums),
        "missing_stems": [q for q in qnums if not stems.get(q)],
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="scripts.author_mcq_paper")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--paper", required=True)
    ap.add_argument("--ms", required=True)
    ap.add_argument("--dry-run", action="store_true", help="print summary, don't write")
    args = ap.parse_args()
    qp_pdf = Path(args.paper).resolve()
    ms_pdf = Path(args.ms).resolve()
    for p in (qp_pdf, ms_pdf):
        if not p.exists():
            raise SystemExit(f"not found: {p}")

    eq, ms, stats = build(args.subject, qp_pdf, ms_pdf)
    print(f"[mcq] {qp_pdf.name}: {stats['n']} questions, "
          f"{len(stats['missing_stems'])} missing stems {stats['missing_stems'] or ''}")
    answers = " ".join(f"{q['number']}{q['correct_answer']}" for q in ms["questions"])
    print(f"[mcq] answers: {answers}")
    sample = eq["questions"][0]
    print(f"[mcq] sample Q1 stem: {sample['text'][:160]!r}")
    if args.dry_run:
        return 0

    out_dir = bank_dir_for(args.subject, qp_pdf)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "exam_questions.yaml").write_text(
        yaml.safe_dump(eq, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    (out_dir / "mark_scheme.yaml").write_text(
        yaml.safe_dump(ms, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    print(f"[mcq] wrote YAMLs → {out_dir}")
    print(f"[mcq] finalize: python -m eXam.bank --authored --paper '{qp_pdf}' "
          f"--ms '{ms_pdf}' --subject {args.subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
