"""AI-cell formatters and oversized-cell splitting.

Composes the primitives in :mod:`report_latex_text` to build the strings that
go into a single ``p{}`` longtable cell:

- :func:`_ai_cell` — turn raw AI-generated text into a render-safe cell body
  (escape, math-wrap, alltt-protect, bullet-convert, ``\\newline`` cleanup).
- :func:`_split_oversized_cell` — when the formatted cell exceeds the
  per-orientation height budget, sub-split it into panels so the longtable
  emits one row per panel (panels stack across page breaks).
- :func:`_awarded_tex` — colour-coded mark cell.
"""

from __future__ import annotations

import re

from xscore.marking.report_latex_text import (
    _ALLTT_BLOCK_RE,
    _ALLTT_MATH_RE,
    _ALLTT_MATH_SUB,
    _LEADING_LIST_ENV_RE,
    _convert_literal_bullets,
    _escape_bare_amp_outside_tabular,
    _protect_alltt,
    _protect_envs,
    _wrap_loose_math,
)


def _ai_cell(text: str, cell_width_cm: float = 3.6) -> str:
    """Prepare AI-generated LaTeX text for a p{} table cell.

    XML element text is stored verbatim (no JSON escaping layer), so no
    control-character restoration is needed.  Literal newlines in the text
    are converted to LaTeX line breaks.

    ``\\newline`` immediately before or after a block-level environment
    (``\\begin{...}`` / ``\\end{...}``) is invalid LaTeX and causes
    "There's no line here to end"; strip those.

    ``\\begin{alltt}…\\end{alltt}`` blocks are passed through unchanged
    (literal newlines, indentation, and bare ``&``/``%``/``<``/``>`` inside
    the block are preserved) so pseudocode keeps its source layout.

    *cell_width_cm* is forwarded to alltt font-size selection so wider cells
    don't shrink unnecessarily. Default 3.6cm matches the original calibration
    target (portrait Student Answer); landscape and with-questions variants
    pass their actual column widths.
    """
    def _outside_alltt(t: str) -> str:
        # Convert math arrows the AI sometimes emits in text mode (e.g. inside
        # \texttt{...}, where _wrap_loose_math can't reach because the whole
        # \texttt{...} is stashed as one unit). Same substitution applied
        # inside alltt by _protect_alltt — running both is idempotent.
        t = _ALLTT_MATH_RE.sub(lambda m: _ALLTT_MATH_SUB[m.group(1)], t)
        # Defensive escape for characters AIs commonly miss when they aren't
        # legitimately part of LaTeX commands. Math ($, \, {, }) is left alone
        # so the AI can still emit `\frac{1}{2}` etc.
        t = re.sub(r"(?<!\\)%", r"\\%", t)         # bare % starts a LaTeX comment
        t = re.sub(r"(?<!\\)#", r"\\#", t)         # bare # is a macro-parameter char
        # Bare `\\` inside a p{} cell terminates the longtable row and shifts
        # subsequent content into the wrong columns. AIs sometimes emit `\\`
        # to mean a line break — convert it to one.
        t = re.sub(r"\\\\\s*", "\n", t)
        t = _escape_bare_amp_outside_tabular(t)    # preserve & inside \begin{tabular}
        t = _wrap_loose_math(t)                    # must precede \n → \newline
        t = re.sub(r"\*\*(.+?)\*\*", r"\\textbf{\1}", t)
        t = _convert_literal_bullets(t)
        t = t.replace("\n", "\\newline ")
        # \newline adjacent to block-level environments is invalid LaTeX
        # ("There's no line here to end") — strip it in all four positions.
        # Match one-or-more so consecutive \newlines (e.g. AI paragraph
        # break `\n\n` → `\newline \newline ` after line `t.replace("\n", ...)`)
        # are all stripped, not just the first.
        # Itemize specifically gets `\par\vspace*{0.5em}` injected (instead of
        # bare strip) so word/option lists are visibly separated from surrounding
        # prose. The starred form is non-discardable: bare `\smallskip` glue
        # before `\begin{itemize}` is absorbed by the list's topsep handling
        # and renders as zero space; `\vspace*{}` survives.
        t = re.sub(r"(?:\\newline\s*)+(?=\\begin\{itemize\})",
                   r"\\par\\vspace*{0.5em}", t)
        t = re.sub(r"(\\end\{itemize\})(?:\s*\\newline\b\s?)+",
                   r"\1\\par\\vspace*{0.5em}", t)
        t = re.sub(r"(?:\\newline\s*)+(?=\\begin\{)", "", t)
        t = re.sub(r"(?<=\})(?:\\newline\s*)+(?=\\begin\{)", "", t)
        t = re.sub(r"(\\begin\{[^}]+\})(?:\s*\\newline\b\s?)+", r"\1", t)
        t = re.sub(r"(\\end\{[^}]+\})(?:\s*\\newline\b\s?)+", r"\1 ", t)
        t = re.sub(r"(?:\\newline\s*)+(?=\\item\b)", "", t)
        t = re.sub(r"(?:\\newline\s*)+(?=\\end\{)", "", t)
        # Uniform gap of length \xanswerlinegap (preamble-tunable) at the two
        # transitions involving \hrulefill answer lines:
        #  (a) between an instruction paragraph and the first answer line
        #      ("Give three reasons:\n\n1. \hrulefill" or
        #       "Convert the two binary numbers:\n\n10010011 \hrulefill")
        #  (b) between consecutive answer lines (1./2./3. or value/value pairs)
        # Both convert to \par\vspace*{\xanswerlinegap}. The starred form is
        # non-discardable so the gap survives glue absorption. Trailing
        # \hrulefill (last in cell, no following \newline) gets no injection.
        t = re.sub(
            r"(?:\\newline\s*){2,}(?=(?:(?!\\newline).)*?\\hrulefill)",
            r"\\par\\vspace*{\\xanswerlinegap} ",
            t,
        )
        t = re.sub(
            r"\\hrulefill(?:\s*\\newline\b\s?)+",
            r"\\hrulefill\\par\\vspace*{\\xanswerlinegap} ",
            t,
        )
        return t

    result = _protect_envs(text, lambda t: _protect_alltt(t, _outside_alltt, cell_width_cm))
    if _LEADING_LIST_ENV_RE.match(result):
        # See _LEADING_LIST_ENV_RE comment for the diagnosis. \vspace*
        # (starred) is non-discardable at the parbox top, where \vspace
        # would be silently dropped. The two \setlength calls zero the
        # trivlist top-of-list spacing for alltt/enumerate; harmless
        # no-op for itemize (whose topsep flows through enumitem, not
        # the underlying length). \setlength is local to the cell's
        # parbox group, so subsequent rows see the original lengths.
        result = (
            r"\setlength{\topsep}{0pt}"
            r"\setlength{\partopsep}{0pt}"
            r"\vspace*{-\baselineskip}"
            + result
        )
    return result


# Per-orientation panel budgets used by `_split_oversized_cell` to detect
# Expected cells whose formatted height would exceed a single longtable row
# (Q12-style mega mark schemes). Units are "prose-line equivalents":
# 10 pt × \arraystretch{1.6} ≈ 16 pt vertical per prose line; alltt lines
# weigh less per the size command (see _ALLTT_SIZE_WEIGHTS below).
# Landscape A4 has ~16 cm of vertical text → ~22 prose lines with margin.
# Portrait A4 is ~28 cm tall → ~40.

_ALLTT_SIZE_WEIGHTS: tuple[tuple[str, float], ...] = (
    (r"\tiny ", 0.4),               # retained for safety; size is retired
    (r"\scriptsize ", 0.40),        # was 0.31 — recalibrated against Cosmo's Q10
                                    # mark-scheme cell after the horizontal-wrap
                                    # tightening (`_ALLTT_SCRIPT_BUDGET_BASE`
                                    # 32→27): the same source content now wraps
                                    # to ~65 rendered lines and overflowed page 9
                                    # of the recalibrated landscape PDF. Math
                                    # check: a landscape A4 page holds ~454 pt of
                                    # body height after margins/footer, and
                                    # scriptsize uses an 8 pt baselineskip
                                    # (size10.clo), giving ~56 scriptsize lines
                                    # max → weight = 22 / 56 ≈ 0.39 → 0.40.
                                    # `_sub_split_alltt_block`'s recursive
                                    # keyword-split is the safety net when the
                                    # weight bump alone leaves a sub-group still
                                    # over budget.
    (r"\footnotesize ", 0.7),
)
_ALLTT_HEADER_RE = re.compile(
    r"\\begin\{alltt\}((?:\\(?:tiny|scriptsize|footnotesize|small|normalsize)\s)?)"
)
# Top-level start-keyword anchors for sub-splitting an oversized alltt block.
# `^` (no `\s*`) requires column-0 indent so nested loops don't qualify —
# only top-level structural starts. Block-opening keywords only; closing
# forms (NEXT/UNTIL/ENDIF/ENDCASE) are intentionally excluded because the
# natural break is *before* a new structure starts, not after one ends.
# IGNORECASE so the AI's lower/title-case variants (`For`, `Repeat`, etc.)
# match alongside the canonical uppercase forms used by mark schemes.
_ALLTT_KEYWORD_RE = re.compile(
    r"^(?:PROCEDURE|FUNCTION|SUBROUTINE|FOR|REPEAT|IF|WHILE|CASE)\b",
    re.IGNORECASE,
)


def _chunk_weight(chunk: str) -> float:
    if r"\begin{alltt}" not in chunk:
        # Each prose chunk is one `\newline`-segment; itemize items expand
        # vertically (one visual line per \item).
        return 1.0 + chunk.count(r"\item ")
    inner = re.sub(r"\\(?:begin|end)\{alltt\}", "", chunk)
    n_lines = inner.count("\n") + 1
    for token, weight in _ALLTT_SIZE_WEIGHTS:
        if token in chunk:
            return n_lines * weight
    return n_lines * 0.85  # body 10pt alltt (no size command)


def _split_prose_lines(prose: str) -> list[str]:
    parts = re.split(r"(\\newline\s)", prose)
    out: list[str] = []
    cur = ""
    for p in parts:
        cur += p
        if p.startswith(r"\newline"):
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def _sub_split_alltt_block(block: str, budget: float = float("inf")) -> list[str]:
    """Sub-split an oversized alltt block at blank lines (preferred) or
    PROCEDURE/FUNCTION/SUBROUTINE keyword boundaries; preserve the parent's
    size command verbatim on each sub-block.

    *budget* allows recursive subdivision: any group emerging from the
    blank-line split that itself still exceeds *budget* is keyword-split
    a second time. Callers passing the panel budget here let
    `_split_oversized_cell` produce panels that actually fit on a page
    when a single blank-line group has too many lines (e.g. Cosmo's Q10
    mark scheme: one DECLARE block + one 50-line FOR-loop body)."""
    h = _ALLTT_HEADER_RE.match(block)
    if not h or not block.endswith(r"\end{alltt}"):
        return [block]
    size_prefix = h.group(1)
    body = block[h.end() : -len(r"\end{alltt}")]
    lines = body.split("\n")

    def _build(group_lines: list[str]) -> str:
        return (
            r"\begin{alltt}" + size_prefix + "\n".join(group_lines) + r"\end{alltt}"
        )

    def _split_blank(line_list: list[str]) -> list[list[str]]:
        out: list[list[str]] = [[]]
        for ln in line_list:
            if ln.strip() == "" and out[-1]:
                out.append([])
            else:
                out[-1].append(ln)
        return [g for g in out if g]

    def _split_keyword(line_list: list[str]) -> list[list[str]]:
        out: list[list[str]] = [[]]
        for ln in line_list:
            if _ALLTT_KEYWORD_RE.match(ln) and out[-1]:
                out.append([])
            out[-1].append(ln)
        return [g for g in out if g]

    groups = _split_blank(lines)
    if len(groups) < 2:
        groups = _split_keyword(lines)

    # Recursive pass: any group at-or-over budget gets keyword-split a
    # second time. `>=` (not `>`) so a group whose weight exactly equals
    # the panel budget still gets subdivided — its presence on a panel
    # would leave zero room for anything else, and the keyword break
    # usually produces pieces that pack better. Skip if the keyword pass
    # produced no further breaks.
    refined: list[list[str]] = []
    for g in groups:
        if _chunk_weight(_build(g)) >= budget:
            sub = _split_keyword(g)
            if len(sub) >= 2:
                refined.extend(sub)
                continue
        refined.append(g)
    groups = refined

    if len(groups) < 2:
        return [block]
    return [_build(g) for g in groups]


def _decompose_cell(cell: str, budget: float):
    pos = 0
    for m in _ALLTT_BLOCK_RE.finditer(cell):
        if pos < m.start():
            yield from _split_prose_lines(cell[pos : m.start()])
        block = m.group(0)
        if _chunk_weight(block) > budget:
            yield from _sub_split_alltt_block(block, budget)
        else:
            yield block
        pos = m.end()
    if pos < len(cell):
        yield from _split_prose_lines(cell[pos:])


def _split_oversized_cell(cell: str, budget: float) -> list[str]:
    """Split a too-tall formatted cell into panel strings so its longtable row
    can break across pages by being emitted as several rows with empty leading
    columns. Returns ``[cell]`` if the cell fits within *budget*."""
    chunks = list(_decompose_cell(cell, budget))
    if sum(_chunk_weight(c) for c in chunks) <= budget:
        return [cell]
    panels: list[list[str]] = [[]]
    used = 0.0
    for c in chunks:
        w = _chunk_weight(c)
        if used > 0 and used + w > budget:
            panels.append([])
            used = 0.0
        panels[-1].append(c)
        used += w
    return ["".join(p) for p in panels if p]


def _awarded_tex(awarded: int | None, max_q: int | str) -> str:
    """Render awarded marks with colour: green=full, red=zero, plain=partial."""
    if awarded is None:
        return "\\textit{?}"
    if awarded == 0:
        return f"\\textcolor{{red!65!black}}{{{awarded}}}"
    try:
        if int(awarded) == int(max_q):
            return f"\\textcolor{{green!55!black}}{{{awarded}}}"
    except (TypeError, ValueError):
        pass
    return str(awarded)
