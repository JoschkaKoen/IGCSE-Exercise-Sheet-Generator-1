"""Author a Cambridge structured-theory paper's bank YAMLs from the PDF text layers.

The mark scheme's "Question / Answer / Marks" table enumerates every gradable part
(``1(a)``, ``1(c)(ii)`` …) with the verbatim marking points — that is the backbone:
it defines the leaf set and the ``mark_scheme_answer`` text. The question paper
supplies per-part marks (``[N]``) and the question text per part.

Produces nested ``exam_questions.yaml`` (top → letter → roman) + flat
``mark_scheme.yaml`` (one entry per gradable leaf), then is finalized with::

    python -m eXam.bank --authored --paper <qp> --ms <ms> --subject <slug>

The practice UI renders the snippet PDF (text isn't shown), and free-response marking
runs against ``mark_scheme_answer`` — so leaf text is best-effort while the scheme
answer is transcribed verbatim. Run with --dry-run to inspect the parse first.

Usage::

    python -m scripts.author_structured_paper --subject igcse_physics \
        --paper "exams/.../...Question Paper  41.pdf" \
        --ms    "exams/.../...Mark Scheme  41.pdf" [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import fitz
import yaml

from eXam.bank import bank_dir_for

# A mark-scheme part label: 1(a) / 1(c)(ii) / 12(b)(i) …  → normalized key "1a"/"1cii".
_PART_RE = re.compile(r"^(\d{1,2})((?:\([a-z]+\))+)\s*$")
_GROUP_RE = re.compile(r"\(([a-z]+)\)")
# Cambridge marks codes in the Marks column (B1, A2, C1, M1, …) — drop from answer text.
_CODE_RE = re.compile(r"^[ABCM]\d+$")
_MARK_RE = re.compile(r"\[(\d{1,2})\]")
_TOTAL_RE = re.compile(r"\[Total:\s*\d+\]")
_NOISE = re.compile(
    r"©|UCLES|Cambridge (University|Assessment|IGCSE)|PUBLISHED|^Page \d|"
    r"Generic Marking|GENERIC MARKING|Mark Scheme|^Question$|^Answer$|^Marks$|"
    r"DO NOT WRITE|/O/N/|/M/J/|/F/M/"
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


def _norm_key(qnum: str, groups: str) -> str:
    """``"1", "(c)(ii)"`` → ``"1cii"``."""
    return qnum + "".join(_GROUP_RE.findall(groups))


def parse_ms_table(ms_pdf: Path) -> list[tuple[str, str]]:
    """Return ``[(leaf_key, answer_text), …]`` in paper order from the MS table.

    Reads the part labels (``1(a)``, ``1(c)(ii)``) as they appear alone on a line
    in the Question column; answer lines follow until the next label. Marks codes
    (``B1``/``A2``) and page furniture are dropped. Bare-number questions (an ATP
    "plan" question labelled just ``2``) are not auto-detected here — those few
    papers are authored directly."""
    lines = _lines(ms_pdf)
    parts: list[tuple[str, list[str]]] = []
    cur: str | None = None
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        m = _PART_RE.match(s)
        if m:
            cur = _norm_key(m.group(1), m.group(2))
            parts.append((cur, []))
            continue
        if cur is None or _CODE_RE.match(s) or _NOISE.search(s):
            continue
        parts[-1][1].append(s)
    merged: dict[str, list[str]] = {}
    order: list[str] = []
    for key, body in parts:
        if key not in merged:
            merged[key] = []
            order.append(key)
        merged[key].extend(body)
    return [(k, "\n".join(merged[k]).strip()) for k in order]


def parse_qp_marks(qp_pdf: Path, leaf_keys: list[str]) -> dict[str, int]:
    """Best-effort per-leaf marks from the QP ``[N]`` annotations, assigned to the
    gradable leaves in order. Falls back to 1 where the count can't be matched."""
    text = "\n".join(_lines(qp_pdf))
    text = _TOTAL_RE.sub("", text)
    marks = [int(x) for x in _MARK_RE.findall(text)]
    out: dict[str, int] = {}
    for i, key in enumerate(leaf_keys):
        out[key] = marks[i] if i < len(marks) else 1
    return out


def _split_key(key: str) -> tuple[str, str, str]:
    """``"1cii"`` → ``("1","c","ii")``; ``"1a"`` → ``("1","a","")``;
    ``"1"`` → ``("1","","")``."""
    m = re.match(r"^(\d{1,2})([a-z])?([ivx]+)?$", key)
    if not m:
        return key, "", ""
    return m.group(1), m.group(2) or "", m.group(3) or ""


def _qtype(answer: str, marks: int) -> str:
    a = answer.lower()
    if re.search(r"\d", answer) and (re.search(r"[=/]|×|·|\^|10\b", answer) or "unit" in a):
        return "calculation"
    return "long_answer" if marks >= 4 else "short_answer"


def build(subject: str, qp_pdf: Path, ms_pdf: Path) -> tuple[dict, dict, dict]:
    ms_parts = parse_ms_table(ms_pdf)
    if not ms_parts:
        raise SystemExit(f"no marking table parsed from {ms_pdf.name}")
    leaf_keys = [k for k, _ in ms_parts]
    answers = dict(ms_parts)
    marks = parse_qp_marks(qp_pdf, leaf_keys)

    # Build the nested exam_questions tree: top → letter → roman.
    tops: dict[str, dict] = {}
    top_order: list[str] = []
    scheme: list[dict] = []
    for key in leaf_keys:
        top, letter, roman = _split_key(key)
        if top not in tops:
            tops[top] = {"number": top, "question_type": "short_answer", "marks": 0,
                         "text": "", "answer_options": [], "subquestions": []}
            top_order.append(top)
        ans = answers[key]
        mk = int(marks.get(key, 1))
        qt = _qtype(ans, mk)
        leaf = {"number": key, "question_type": qt, "marks": mk,
                "text": "", "answer_options": [], "subquestions": []}
        node = tops[top]
        if letter:
            # find/create the letter node
            lkey = top + letter
            lnode = next((c for c in node["subquestions"] if c["number"] == lkey), None)
            if roman:
                if lnode is None:
                    lnode = {"number": lkey, "question_type": "short_answer", "marks": 0,
                             "text": "", "answer_options": [], "subquestions": []}
                    node["subquestions"].append(lnode)
                lnode["subquestions"].append(leaf)
            else:
                # letter is itself the leaf
                if lnode is None:
                    node["subquestions"].append(leaf)
                else:  # letter already a container — attach as its own leaf row
                    lnode.update({k: leaf[k] for k in ("question_type", "marks")})
        else:
            # standalone top-level leaf (no parts): the top *is* the leaf
            node.update({"question_type": qt, "marks": mk})
        scheme.append({"number": key, "question_type": qt, "correct_answer": None,
                       "explanation": None, "mark_scheme_answer": ans,
                       "mark_scheme": [], "graphics": []})

    questions = [tops[t] for t in top_order]

    # Self-check: per-question leaf-mark sums must match the QP [Total: N] markers,
    # which catches any drift in the sequential [N]→leaf assignment.
    qp_totals = [int(x) for x in re.findall(r"\[Total:\s*(\d+)\]", "\n".join(_lines(qp_pdf)))]
    def _leafsum(node: dict) -> int:
        if node["subquestions"]:
            return sum(_leafsum(c) for c in node["subquestions"])
        return node["marks"]
    mismatches = []
    for i, q in enumerate(questions):
        if i < len(qp_totals) and _leafsum(q) != qp_totals[i]:
            mismatches.append((q["number"], _leafsum(q), qp_totals[i]))

    stats = {"tops": len(top_order), "leaves": len(leaf_keys),
             "total_marks": sum(marks.values()),
             "empty_answers": [k for k in leaf_keys if not answers[k]],
             "mark_mismatches": mismatches}
    return {"questions": questions}, {"questions": scheme}, stats


def main() -> int:
    ap = argparse.ArgumentParser(prog="scripts.author_structured_paper")
    ap.add_argument("--subject", required=True)
    ap.add_argument("--paper", required=True)
    ap.add_argument("--ms", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    qp_pdf, ms_pdf = Path(args.paper).resolve(), Path(args.ms).resolve()
    for p in (qp_pdf, ms_pdf):
        if not p.exists():
            raise SystemExit(f"not found: {p}")

    eq, ms, stats = build(args.subject, qp_pdf, ms_pdf)
    print(f"[struct] {qp_pdf.name}: {stats['tops']} questions, {stats['leaves']} leaves, "
          f"total marks {stats['total_marks']}, empty answers {stats['empty_answers'] or 'none'}")
    if stats["mark_mismatches"]:
        print(f"[struct] WARNING mark/total mismatches (q, computed, QP-total): "
              f"{stats['mark_mismatches']} — review before finalizing")
    for q in eq["questions"]:
        leaves = []
        def walk(n):
            if n["subquestions"]:
                for c in n["subquestions"]:
                    walk(c)
            else:
                leaves.append(f"{n['number']}[{n['marks']}]")
        walk(q)
        print(f"   Q{q['number']}: {' '.join(leaves)}")
    print(f"   sample scheme [{ms['questions'][0]['number']}]: "
          f"{ms['questions'][0]['mark_scheme_answer'][:120]!r}")
    if args.dry_run:
        return 0

    out_dir = bank_dir_for(args.subject, qp_pdf)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "exam_questions.yaml").write_text(
        yaml.safe_dump(eq, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (out_dir / "mark_scheme.yaml").write_text(
        yaml.safe_dump(ms, sort_keys=False, allow_unicode=True), encoding="utf-8")
    print(f"[struct] wrote YAMLs → {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
