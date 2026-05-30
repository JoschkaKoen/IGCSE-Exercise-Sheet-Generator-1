# -*- coding: utf-8 -*-
"""Per-topic vocab glossary → xelatex longtable PDF (deterministic, no AI).

Renders a topic's ``<NN>.glossary.tsv`` (english / 简体中文 / pinyin) as a paginating
three-column table. Reuses the handout preamble (``web.handout_latex.build_preamble`` —
fontspec Latin Modern for Latin **and** pinyin diacritics, ``xeCJK`` + FandolSong for CJK)
so vocab PDFs match the handout PDFs visually; adds only ``longtable`` on top (with the
``parskip`` spacing reset). Latin Modern covers every pinyin codepoint present in the
corpus, so no ``\\setmainfont`` override is needed.
"""

from __future__ import annotations

from typing import Any

from .handout_latex import build_preamble, escape_latex

__all__ = ["build_vocab_document", "build_vocab_preamble"]

# English | 中文 | pinyin. ``@{}`` trims the outer padding; the three fractions plus the
# two inter-column gaps stay under ``\linewidth`` so rows never overflow the text block.
# Fixed ``p{}`` widths mean longtable needs no rerun to settle column widths.
_COLSPEC = r"@{}p{0.40\linewidth}p{0.24\linewidth}p{0.30\linewidth}@{}"


def build_vocab_preamble() -> str:
    """Handout preamble + ``longtable`` (with the parskip↔longtable spacing reset)."""
    return build_preamble() + "\n" + "\n".join(
        [
            r"\usepackage{longtable}",
            # parskip injects \parskip glue around a longtable → stray gaps / a blank
            # first page. Reset the longtable's own pre/post spacing.
            r"\setlength\LTpre{0pt}",
            r"\setlength\LTpost{\baselineskip}",
        ]
    )


def _subject_display(subject_key: str) -> str:
    # Mirror web.handout_latex._subject_display so the title block matches handout PDFs.
    return subject_key.replace("_", " ").title()


def build_vocab_document(
    rows: list[tuple[str, str, str]],
    *,
    subject: str,
    topic: str,
    meta: dict[str, Any],
) -> tuple[str, list[str]]:
    """Full standalone ``.tex`` for a topic's vocab list. Returns (tex, warnings)."""
    meta = meta or {}
    warnings: list[str] = []
    title = str(meta.get("topic_title") or f"Topic {topic}")
    title_block = (
        "\\begin{center}\n"
        f"  {{\\LARGE\\bfseries {escape_latex(title)}}}\\\\[0.35em]\n"
        f"  {{\\large {escape_latex(_subject_display(subject))} · Vocabulary}}\n"
        "\\end{center}\n\\vspace{0.6em}\n\n"
    )

    lines = [
        f"\\begin{{longtable}}{{{_COLSPEC}}}",
        r"\toprule",
        r"\textbf{English} & \textbf{中文} & \textbf{Pinyin} \\",
        r"\midrule",
        r"\endhead",
        r"\bottomrule",
        r"\endlastfoot",
    ]
    if not rows:
        warnings.append("empty glossary")
    for en, zh, pinyin in rows:
        # Escape every cell — CJK passes through escape_latex untouched; this guards a
        # stray & / % / _ in a term (e.g. a future chemistry entry).
        lines.append(f"{escape_latex(en)} & {escape_latex(zh)} & {escape_latex(pinyin)} \\\\")
    lines.append(r"\end{longtable}")
    body = "\n".join(lines) + "\n"

    tex = (
        build_vocab_preamble()
        + "\n\n\\begin{document}\n\n"
        + title_block
        + body
        + "\n\\end{document}\n"
    )
    return tex, warnings
