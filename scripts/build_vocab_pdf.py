# -*- coding: utf-8 -*-
"""Build printable vocab-list PDFs from per-topic glossaries via xelatex.

Deterministic, no AI: each ``output/eXam/handouts/<subject>/<NN>.glossary.tsv`` is
rendered to ``pdf/<NN>.vocab.tex`` (web.vocab_latex) and compiled to ``pdf/<NN>.vocab.pdf``.
Both are committed; the website serves the live TSV as an HTML table and links the PDF.

Usage:
  .venv/bin/python -m scripts.build_vocab_pdf <subject> [topic]   # one subject, or one NN
  .venv/bin/python -m scripts.build_vocab_pdf --all               # every subject with a glossary
  .venv/bin/python -m scripts.build_vocab_pdf <subject> [topic] --no-regen  # compile existing .tex

A ``% handout-pdf: manual`` sentinel on line 1 of a ``.vocab.tex`` protects hand-edits:
that file is never regenerated; ``--no-regen`` compiles the existing ``.tex`` as-is.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts._latex_build import compile_tex, source_date_epoch
from web.handouts_collect import (
    handout_dir,
    load_glossary,
    load_meta,
    logs_dir,
    meta_path,
    padded_topic,
    vocab_pdf_path,
    vocab_subjects,
    vocab_tex_path,
)
from web.vocab_latex import build_vocab_document

MANUAL_SENTINEL = "% handout-pdf: manual"


def _topics(subject: str) -> list[str]:
    return sorted(
        p.name.split(".")[0] for p in handout_dir(subject).glob("[0-9][0-9].glossary.tsv")
    )


def _is_manual(tex_file: Path) -> bool:
    if not tex_file.is_file():
        return False
    try:
        with tex_file.open(encoding="utf-8") as f:
            return f.readline().strip() == MANUAL_SENTINEL
    except OSError:
        return False


def _build_one(subject: str, topic: str, *, no_regen: bool) -> tuple[bool, list[str], str]:
    tex_file = vocab_tex_path(subject, topic)
    meta = load_meta(meta_path(subject, topic))
    warnings: list[str] = []
    protected = _is_manual(tex_file)
    if not no_regen and not protected:
        rows = load_glossary(subject, topic)
        if rows is None:
            return False, warnings, "no .glossary.tsv source"
        tex, warnings = build_vocab_document(rows, subject=subject, topic=topic, meta=meta)
        tex_file.parent.mkdir(parents=True, exist_ok=True)
        tex_file.write_text(tex, encoding="utf-8")
    elif not tex_file.is_file():
        return False, warnings, "no .tex to compile (run without --no-regen first)"
    ok, err = compile_tex(
        tex_file,
        vocab_pdf_path(subject, topic),
        sde=source_date_epoch(meta),
        log_target=logs_dir(subject, topic) / "vocab_xelatex.log",
        prefix="vocab_pdf_",
    )
    if protected and ok:
        warnings.append("manual .tex (not regenerated)")
    return ok, warnings, err


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    no_regen = "--no-regen" in argv
    argv = [a for a in argv if a != "--no-regen"]

    if argv == ["--all"]:
        targets = [(s, t) for s in vocab_subjects() for t in _topics(s)]
    elif argv and not argv[0].startswith("--"):
        subject = argv[0]
        if not handout_dir(subject).is_dir():
            print(f"no such subject dir: {handout_dir(subject)}", file=sys.stderr)
            return 2
        targets = [(subject, argv[1])] if len(argv) > 1 else [(subject, t) for t in _topics(subject)]
    else:
        print(__doc__, file=sys.stderr)
        return 2

    failures = warned = 0
    for subject, topic in targets:
        ok, warnings, err = _build_one(subject, topic, no_regen=no_regen)
        pt = padded_topic(topic)
        if ok:
            warned += 1 if warnings else 0
            suffix = f"  [{'; '.join(warnings)}]" if warnings else ""
            print(f"{'✓!' if warnings else '✓'} {subject}/{pt}.vocab.pdf{suffix}")
        else:
            failures += 1
            print(f"✗ {subject}/{pt}.glossary.tsv  — {err}")
    print(
        f"\nBuilt {len(targets) - failures}/{len(targets)} vocab PDF(s); "
        f"{failures} failed; {warned} with warnings."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
