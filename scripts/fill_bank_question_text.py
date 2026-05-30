#!/usr/bin/env python3
"""Deterministically backfill empty ``text`` fields in the eXam bank.

The Learn "Extracted exam questions" page renders a question body only when
``exam_questions.yaml`` carries non-empty ``text``. Roughly half the banked
papers were indexed by a path that left ``text`` empty (they lack the xscore
step-artifact dirs and only carry structure + marks), so the page shows blank
question bodies for them.

This script fills those gaps WITHOUT any AI / API call. Each top-level question
already has a pre-segmented vector snippet at ``<paper>/<N>/question.pdf`` (cut
by the non-AI ``layout_vector_strips_to_pdf`` path). We read that snippet's
text layer with PyMuPDF and store it on the top-level node.

Rules (idempotent, conservative):
  * Only a top-level question whose ENTIRE subtree has empty text is touched —
    so papers/questions that already show text are never altered or duplicated.
  * Only the top-level node is filled (the snippet is the whole question); the
    subquestion rows keep showing their number + marks beneath it.
  * Non-integer top-level numbers (no snippet), missing snippets, and
    image-only snippets (no text layer) are reported, not guessed.

Usage:
  python -m scripts.fill_bank_question_text --dry-run --sample   # report only
  python -m scripts.fill_bank_question_text --subject a_level_physics
  python -m scripts.fill_bank_question_text                      # apply to all
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import fitz
import yaml

from eXam.bank import BANK_ROOT


def _subtree_has_text(q: dict) -> bool:
    if str(q.get("text") or "").strip():
        return True
    for s in q.get("subquestions") or []:
        if isinstance(s, dict) and _subtree_has_text(s):
            return True
    return False


_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(txt: str) -> str:
    """Tidy a raw PDF text layer for display (no semantic change)."""
    txt = _CTRL_RE.sub("", txt)                  # stray control chars (e.g. \x08)
    txt = re.sub(r"(?:\.\s*){4,}", " ", txt)     # collapse Cambridge answer dot-lines
    txt = re.sub(r"[ \t]+\n", "\n", txt)         # trailing whitespace
    txt = re.sub(r"\n{3,}", "\n\n", txt)         # runs of blank lines
    return txt.strip()


def _snippet_text(paper_dir: Path, qnum_int: int) -> str | None:
    """Text layer of ``<paper>/<N>/question.pdf``.

    ``None`` => snippet missing/unreadable; ``""`` => snippet has no text layer.
    """
    snip = paper_dir / str(qnum_int) / "question.pdf"
    if not snip.exists():
        return None
    try:
        doc = fitz.open(snip)
        txt = "\n".join(page.get_text() for page in doc)
        doc.close()
    except Exception:  # noqa: BLE001 — corrupt snippet should not abort the run
        return None
    return _clean(txt)


def fill_paper(paper_dir: Path, dry: bool) -> dict:
    yp = paper_dir / "exam_questions.yaml"
    try:
        data = yaml.safe_load(yp.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {"error": str(exc)}
    qs = data.get("questions") or []
    st = {"top": len(qs), "filled": 0, "skip_has_text": 0,
          "no_snippet": 0, "snippet_empty": 0, "bad_num": 0, "sample": None}
    for q in qs:
        if not isinstance(q, dict):
            continue
        if _subtree_has_text(q):
            st["skip_has_text"] += 1
            continue
        try:
            qint = int(q.get("number"))
        except (TypeError, ValueError):
            st["bad_num"] += 1
            continue
        txt = _snippet_text(paper_dir, qint)
        if txt is None:
            st["no_snippet"] += 1
            continue
        if not txt:
            st["snippet_empty"] += 1
            continue
        if not dry:
            q["text"] = txt
        st["filled"] += 1
        if st["sample"] is None:
            st["sample"] = (q.get("number"), txt[:240])
    if st["filled"] and not dry:
        yp.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return st


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m scripts.fill_bank_question_text")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--subject", default=None, help="limit to one subject slug")
    ap.add_argument("--sample", action="store_true",
                    help="print a sample of extracted text per subject")
    a = ap.parse_args(argv)

    if not BANK_ROOT.exists():
        print(f"bank not found: {BANK_ROOT}", file=sys.stderr)
        return 2
    subjects = [a.subject] if a.subject else sorted(
        d.name for d in BANK_ROOT.iterdir() if d.is_dir())

    g = {"filled": 0, "pchg": 0, "no_snippet": 0, "snippet_empty": 0}
    for subj in subjects:
        sd = BANK_ROOT / subj
        if not sd.is_dir():
            print(f"{subj}: no such subject dir", file=sys.stderr)
            continue
        papers = sorted(d for d in sd.iterdir()
                        if d.is_dir() and (d / "exam_questions.yaml").exists())
        s_filled = s_no = s_empty = s_pchg = 0
        sample = None
        for pd in papers:
            st = fill_paper(pd, a.dry_run)
            if st.get("error"):
                print(f"  ! {subj}/{pd.name}: {st['error']}", file=sys.stderr)
                continue
            if st["filled"]:
                s_pchg += 1
            s_filled += st["filled"]
            s_no += st["no_snippet"]
            s_empty += st["snippet_empty"]
            if sample is None and st["sample"]:
                sample = (pd.name, st["sample"])
        print(f"{subj:30s} papers={len(papers):3d} filled_q={s_filled:5d} "
              f"papers_changed={s_pchg:3d} no_snippet={s_no:4d} "
              f"snippet_empty={s_empty:4d}")
        if a.sample and sample:
            pn, (qn, preview) = sample
            print(f"    e.g. {pn} q{qn}: {preview!r}")
        for k, v in (("filled", s_filled), ("pchg", s_pchg),
                     ("no_snippet", s_no), ("snippet_empty", s_empty)):
            g[k] += v

    mode = "DRY-RUN" if a.dry_run else "APPLIED"
    print(f"\n[{mode}] filled_q={g['filled']} papers_changed={g['pchg']} "
          f"no_snippet={g['no_snippet']} snippet_empty={g['snippet_empty']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
