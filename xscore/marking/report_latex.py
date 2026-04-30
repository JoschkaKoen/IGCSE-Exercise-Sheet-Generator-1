"""LaTeX formatting helpers and serializers for student and class reports.

LaTeX skeletons live as Jinja2 templates in ``./templates/``. This module
prepares every dynamic value as a Python string (rows, header substrings,
geometry/column-spec lines) and then substitutes them into the template via
``<<var>>`` placeholders. No control flow happens inside the templates —
that keeps Jinja's whitespace handling out of the loop and makes byte-diffs
against the original f-string serializers easy to reason about.

Jinja delimiters here are ``<< >>`` for variables, ``<% %>`` for blocks,
``<# #>`` for comments — chosen to avoid clashing with LaTeX braces.
"""

from __future__ import annotations

import datetime
import math
import re
from pathlib import Path

import jinja2


_LATEX_MAP = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "<": r"\textless{}",
    ">": r"\textgreater{}",
}
_LATEX_RE = re.compile("|".join(re.escape(k) for k in _LATEX_MAP))


def _latex_escape(text: str) -> str:
    """Escape special LaTeX characters (single-pass to avoid double-escaping)."""
    return _LATEX_RE.sub(lambda m: _LATEX_MAP[m.group()], text)


_TABULAR_RE = re.compile(r"(\\begin\{tabular\*?\}.*?\\end\{tabular\*?\})", re.DOTALL)


# Detect AI-emitted bullet runs in `_ai_cell`. Lines starting with `-` or
# `•` (followed by whitespace + non-space content) are wrapped in a real
# itemize block, so the prompt's intended list structure survives even
# when the model forgets to use \begin{itemize}. Markers kept narrow so
# the regex never collides with prose (`-2`, em-dash interjections, etc).
# Split on both real `\n` (introduced by `\\` -> `\n` upstream in _ai_cell)
# and the literal `\newline` text the AI emits per the FIELD_RULES rule.
_LINE_BREAK_RE = re.compile(r"\n|\\newline\s*")
_BULLET_LINE_RE = re.compile(r"^\s*[-•]\s+(\S.*)$")


# Cells whose content begins with itemize / enumerate / alltt show
# leading whitespace observed visually in the rendered PDF
# (Simon_Wang_landscape.pdf Q2/Q3/Q4a Reasoning, Q4a/Q4bii Student Answer).
# Two contributors, handled together at cell start in _ai_cell:
#   1) ~1 baseline of trivlist-related offset that the templates'
#      \setlist[itemize]{topsep=0pt,partopsep=0pt} cancels for itemize via
#      enumitem, but cannot cancel for alltt/enumerate (those go through
#      plain \trivlist and read the underlying \topsep / \partopsep
#      lengths directly). Cancelled with \vspace*{-\baselineskip}.
#   2) For alltt/enumerate specifically, the trivlist also reads
#      \topsep + \partopsep at \begin time and inserts ~10pt of space
#      that the pull-up alone cannot reach (visible as a free line above
#      pseudocode). Cancelled by setting both lengths to 0pt locally
#      before the env opens; harmless for itemize (enumitem doesn't
#      consult the underlying lengths). enumerate is included for
#      defence-in-depth — not currently emitted by the AI but mark
#      schemes may use it in future exams.
_LEADING_LIST_ENV_RE = re.compile(r"^\s*\\begin\{(?:itemize|enumerate|alltt)\b")


def _escape_bare_amp_outside_tabular(text: str) -> str:
    """Escape bare ``&`` to ``\\&`` everywhere except inside
    ``\\begin{tabular}…\\end{tabular}``, where ``&`` is the column separator."""
    parts = _TABULAR_RE.split(text)
    out = []
    for seg in parts:
        if seg.startswith(r"\begin{tabular"):
            out.append(seg)
        else:
            out.append(re.sub(r"(?<!\\)&", r"\\&", seg))
    return "".join(out)


_ALLTT_BLOCK_RE = re.compile(r"\\begin\{alltt\}.*?\\end\{alltt\}", re.DOTALL)
_ALLTT_PLACEHOLDER_RE = re.compile(r"\x00ALLTT(\d+)\x00")

# AI sometimes emits math arrows in text mode. Originally seen inside alltt
# pseudocode (`P \leftarrow "x"`) — alltt is text-mode only, so xelatex would
# insert an implicit `$` and then error at `\end{alltt}` ("invalid in math
# mode"). Also seen inside `\texttt{...}` in AI explanations, where
# `_wrap_loose_math` can't reach (the whole `\texttt{...}` is stashed as a
# single text-command unit). Substitute with Unicode in both contexts —
# fontspec renders these glyphs directly, identically in text and math modes.
_ALLTT_MATH_SUB = {
    "leftarrow": "←", "gets": "←",
    "rightarrow": "→", "to": "→",
    "Leftarrow": "⇐",
    "Rightarrow": "⇒",
}
# Match either the raw form `\leftarrow` or the prompt-escaped form
# `\textbackslash{}leftarrow`. Step 22's mark-scheme parsing prompt tells the
# AI to escape backslashes inside alltt to `\textbackslash{}`, so pseudocode
# arrows arrive in either form depending on the AI's mood. Trailing whitespace
# is left alone — alltt preserves spaces verbatim, and consuming one would
# render `P \leftarrow "x"` as `P ←"x"` (no gap after the arrow).
_ALLTT_MATH_RE = re.compile(
    r"(?:\\textbackslash\{\}|\\)(" + "|".join(_ALLTT_MATH_SUB) + r")\b"
)


# Font-size step-down for alltt blocks whose longest line would overflow the
# cell. The empirical thresholds (20 / 32 / 47 chars) were calibrated against
# /tmp/adjustbox_test/test4.pdf in a 3.6cm cell — the narrowest column that
# hosts alltt (portrait Student Answer):
#   - body 10pt: ~5.7 chars/cm  → fits up to ~20 chars before overflow
#   - \footnotesize ~8pt: ~9.4 chars/cm → up to ~32 chars
#   - \scriptsize ~7pt: ~12 chars/cm → up to ~47 chars
#   - \tiny ~5pt: ~17 chars/cm → up to ~55 chars (floor; readability cost)
# For wider cells (e.g. 7cm landscape Expected) the thresholds scale linearly
# by `cell_width_cm / 3.6`. math.ceil absorbs the discreteness of integer
# char counts vs continuous chars/cm — at 7cm a 39-char line measures
# 7×(20/3.6) = 38.89cm-equivalent which JUST exceeds the linear-extrapolated
# body threshold; the original 3.6cm calibration "<= 20" was almost certainly
# a rounded-down conservative value, so ceiling is the honest interpretation.
# Default cell_width_cm=3.6 reproduces the original behaviour exactly:
# ceil(20*1.0)=20, ceil(32*1.0)=32, ceil(47*1.0)=47.
def _alltt_size_command(block: str, cell_width_cm: float = 3.6) -> str:
    inner = re.sub(r"\\(?:begin|end)\{alltt\}", "", block)
    inner = inner.replace(r"\textbackslash{}", "\\")  # 16-char escape → 1
    max_len = max((len(ln) for ln in inner.split("\n")), default=0)
    scale = cell_width_cm / 3.6
    if max_len <= math.ceil(20 * scale):
        return ""
    if max_len <= math.ceil(32 * scale):
        return "\\footnotesize "
    if max_len <= math.ceil(47 * scale):
        return "\\scriptsize "
    return "\\tiny "


# Long pseudocode lines containing AND/OR (boolean expressions) are broken
# after the first such operator before `_alltt_size_command` measures, so the
# size selection sees the post-break (shorter) longest line and can keep the
# block at \footnotesize instead of dropping to \scriptsize / \tiny.
# Threshold = `\footnotesize` ceiling (`ceil(32 * scale)`); above this the
# block would currently shrink. The break is single-pass per line — the user
# asked for "after the first occurrence", and a second break inside a
# bool-expression continuation rarely buys more readability.
_ALLTT_OP_BREAK_RE = re.compile(r"\s(AND|OR)\s")


def _break_alltt_long_lines(block: str, cell_width_cm: float = 3.6) -> str:
    m = re.match(r"(\\begin\{alltt\})(.*)(\\end\{alltt\})", block, re.DOTALL)
    if not m:
        return block
    prefix, body, suffix = m.groups()
    threshold = math.ceil(32 * cell_width_cm / 3.6)
    new_lines: list[str] = []
    for line in body.split("\n"):
        effective = len(line.replace(r"\textbackslash{}", "\\"))
        if effective <= threshold:
            new_lines.append(line)
            continue
        op_m = _ALLTT_OP_BREAK_RE.search(line)
        if not op_m:
            new_lines.append(line)
            continue
        leading = re.match(r"^\s*", line).group(0)
        head = line[: op_m.end()].rstrip()
        tail = leading + "  " + line[op_m.end() :].lstrip()
        new_lines.append(head)
        new_lines.append(tail)
    return prefix + "\n".join(new_lines) + suffix


# AI-generated cells sometimes embed `\begin{tabular}…\end{tabular}` (truth
# tables, mark-scheme tables). The post-munging passes in `_ai_cell` would
# convert the tabular's `\\` row terminators to `\newline` and break alignment.
# Stash these blocks before munging, restore byte-identically afterwards.
_PROTECTED_ENV_NAMES = r"tabular\*?|array|pmatrix|bmatrix|cases|aligned"
_ENV_BLOCK_RE = re.compile(
    # Tempered token ``(?:(?!\\begin{\1}).)*?`` blocks the body from spanning
    # a same-name nested ``\begin{NAME}``, so a self-nested env (tabular in
    # tabular) matches innermost-first. Without it the non-greedy ``.*?``
    # would match outer-begin..first-inner-end, mis-aligning boundaries that
    # downstream restore + escape passes can't recover from.
    r"\\begin\{(" + _PROTECTED_ENV_NAMES + r")\}"
    r"(?:(?!\\begin\{\1\}).)*?"
    r"\\end\{\1\}",
    re.DOTALL,
)
_ENV_PLACEHOLDER_RE = re.compile(r"\x00ENV(\d+)\x00")

# Inner ``\begin{tabular}…\end{tabular}`` blocks in p{} cells visually butt
# against the outer longtable header (when at cell start) and against
# preceding prose (when AI emits ``\newline\begin{tabular}``). Wrap with a
# small vspace on restore. ``\par\addvspace`` is the only idiom that works in
# all three positions: cell-start, after prose, and before more prose.
# (``\vspace*`` mid-paragraph is silently discarded — empirically verified.)
_TABULAR_VSPACE = "0.5em"


# AI sometimes emits LaTeX math syntax outside ``$...$`` despite the prompt
# warnings. xelatex then errors ("Missing \cr inserted", "Missing $ inserted",
# "Undefined control sequence") and crashes the whole student report.
# ``_wrap_loose_math`` is the render-time safety net: it finds maximal "math
# runs" outside existing ``$...$`` regions and, if a run contains a math
# indicator (``^``, ``_``, or a known math-only command), wraps it in dollars.
#
# Text-mode commands like ``\textbf{...}``, ``\newline``, ``\begin{itemize}`` are
# stashed first so they cannot extend a math run or have their brace arguments
# misread as standalone math.
_TEXT_CMDS = (
    r"newline|begin|end|item|textbf|textit|texttt|textsf|textrm"
    r"|emph|hline|cr|noindent|par|textcolor|textbullet|textbackslash"
    r"|textasciicircum|textasciitilde|textless|textgreater"
)
_MATH_CMDS = (
    r"alpha|beta|gamma|delta|epsilon|zeta|eta|theta|iota|kappa"
    r"|lambda|mu|nu|xi|omicron|pi|rho|sigma|tau|upsilon|phi|chi|psi|omega"
    r"|varepsilon|vartheta|varphi|varpi|varrho|varsigma"
    r"|Alpha|Beta|Gamma|Delta|Epsilon|Zeta|Eta|Theta|Iota|Kappa"
    r"|Lambda|Mu|Nu|Xi|Omicron|Pi|Rho|Sigma|Tau|Upsilon|Phi|Chi|Psi|Omega"
    r"|times|cdot|div|pm|mp|neq|leq|geq|approx|equiv|propto|sim|simeq|cong"
    r"|rightarrow|leftarrow|to|mapsto|Rightarrow|Leftarrow"
    r"|Leftrightarrow|leftrightarrow"
    r"|sin|cos|tan|cot|sec|csc|sinh|cosh|tanh|log|ln|exp"
    r"|sqrt|frac|dfrac|tfrac|sum|prod|int|oint|lim|max|min|inf|sup|binom"
    r"|infty|emptyset|in|notin|subset|supset|cup|cap"
    r"|forall|exists|nabla|partial"
    r"|angle|perp|parallel|circ|deg|prime|dagger|degree"
    r"|text|mathrm|mathbf|mathit|mathcal|mathbb|mathsf|mathtt|operatorname"
    r"|quad|qquad"
)
# Brace group, up to 2 levels of nesting (handles `\frac{a}{b}`, `^{x_{1}}`).
_BRACE = r"\{(?:[^{}]|\{[^{}]*\})*\}"

_TEXT_CMD_RE = re.compile(rf"\\(?:{_TEXT_CMDS})\b(?:{_BRACE})*")
_DOLLAR_SPLIT_RE = re.compile(r"((?<!\\)\$[^$\n]*(?<!\\)\$)")
_MATH_RUN_RE = re.compile(
    rf"""
    (?:
        \\[A-Za-z]+(?:{_BRACE})*    # \cmd{{args}} (text-mode ones already stashed)
      | [\^_]{_BRACE}               # ^{{x}} or _{{x}}
      | [\^_][A-Za-z0-9]            # ^x or _x (single char)
      | {_BRACE}                    # bare brace group
      | [A-Za-z0-9+\-*/=().,]       # alphanum / operators
    )+
    """,
    re.VERBOSE,
)
_MATH_INDICATOR_RE = re.compile(rf"[\^_]|\\(?:{_MATH_CMDS})\b")
_STASH_RE = re.compile(r"\x00TXT(\d+)\x00")


def _wrap_loose_math(text: str) -> str:
    stashed: list[str] = []

    def _stash(m: re.Match) -> str:
        stashed.append(m.group(0))
        return f"\x00TXT{len(stashed) - 1}\x00"

    text = _TEXT_CMD_RE.sub(_stash, text)
    parts = _DOLLAR_SPLIT_RE.split(text)
    for i, part in enumerate(parts):
        if i % 2 == 0:
            # Bare `$` left in an "outside" part has no closing `$` on the
            # same line — it's currency (mark schemes write "(cost =) $36"),
            # not the start of math mode. Escape so xelatex doesn't open
            # math mode and crash on text-only commands like `\newline` that
            # follow on the next line.
            part = re.sub(r"(?<!\\)\$", r"\\$", part)
            parts[i] = _MATH_RUN_RE.sub(_maybe_wrap_math, part)
    text = "".join(parts)
    return _STASH_RE.sub(lambda m: stashed[int(m.group(1))], text)


def _maybe_wrap_math(m: re.Match) -> str:
    run = m.group(0)
    if _MATH_INDICATOR_RE.search(run):
        return f"${run.rstrip()}$"
    return run


def _protect_alltt(text: str, transform, cell_width_cm: float = 3.6) -> str:
    """Run *transform* on parts of *text* outside ``\\begin{alltt}…\\end{alltt}``.

    Inside alltt only a leading newline immediately after ``\\begin{alltt}`` and
    trailing whitespace immediately before ``\\end{alltt}`` are trimmed (so the
    block doesn't render with a blank first line); literal newlines, indentation,
    and bare ``&``/``%``/``<``/``>`` inside the block are preserved exactly.

    *cell_width_cm* is forwarded to `_alltt_size_command` so the alltt font-size
    step-down scales linearly with the host cell's width. Default 3.6cm matches
    the original calibration target (portrait Student Answer column).
    """
    stashed: list[str] = []

    def _stash(m: re.Match) -> str:
        stashed.append(m.group(0))
        return f"\x00ALLTT{len(stashed) - 1}\x00"

    text = _ALLTT_BLOCK_RE.sub(_stash, text)
    text = transform(text)

    def _restore(m: re.Match) -> str:
        block = stashed[int(m.group(1))]
        block = re.sub(r"(\\begin\{alltt\})\n", r"\1", block, count=1)
        block = re.sub(r"\s*(\\end\{alltt\})", r"\1", block, count=1)
        block = _ALLTT_MATH_RE.sub(lambda mm: _ALLTT_MATH_SUB[mm.group(1)], block)
        block = _break_alltt_long_lines(block, cell_width_cm)
        size = _alltt_size_command(block, cell_width_cm)
        if size:
            block = block.replace(r"\begin{alltt}", r"\begin{alltt}" + size, 1)
        return block

    text = _ALLTT_PLACEHOLDER_RE.sub(_restore, text)
    # The strip rules in `transform` couldn't see `\begin{alltt}` / `\end{alltt}`
    # while they were stashed as placeholders. Re-apply them post-restore so
    # `\end{alltt}\newline` ("There's no line here to end") and
    # `\newline\begin{alltt}` get cleaned up.
    text = re.sub(r"(\\end\{alltt\})\s*\\newline\b\s?", r"\1 ", text)
    text = re.sub(r"\\newline\s*(?=\\begin\{alltt\})", "", text)
    return text


def _protect_envs(text: str, transform) -> str:
    """Run *transform* on parts of *text* outside protected env blocks
    (``\\begin{tabular}…\\end{tabular}`` and other tabular/math envs in
    ``_PROTECTED_ENV_NAMES``). Stashed blocks are restored byte-identically
    (internal ``\\\\``, ``\\hline``, ``&``, and newlines preserved exactly),
    except ``tabular`` / ``tabular*`` blocks are wrapped with
    ``{\\par\\addvspace{...}}`` separators so they don't visually butt
    against the surrounding longtable header or adjacent prose.
    """
    stashed: list[str] = []

    def _stash(m: re.Match) -> str:
        stashed.append(m.group(0))
        return f"\x00ENV{len(stashed) - 1}\x00"

    # Fixed-point loop covers nested same-name blocks (tabular-in-tabular).
    for _ in range(5):
        new_text = _ENV_BLOCK_RE.sub(_stash, text)
        if new_text == text:
            break
        text = new_text

    text = transform(text)

    # Indices visible at top level here are the outermost-stashed blocks.
    # Inner blocks live inside other stashed blocks and become visible only
    # after their parent restore expands. Snapshot before restoring so the
    # \par\addvspace wrap fires on outers only — \par inside a tabular cell
    # breaks row alignment.
    top_level = {m.group(1) for m in _ENV_PLACEHOLDER_RE.finditer(text)}

    def _restore(m: re.Match) -> str:
        idx = m.group(1)
        block = stashed[int(idx)]
        if block.startswith((r"\begin{tabular}", r"\begin{tabular*}")):
            # [t] sets the tabular's reference point to its top-row baseline
            # so the visible top of the first row aligns with the first
            # character of any prose/itemize neighbour cell in the same
            # longtable row. Default [c] (centre) plus \adjustbox would
            # leave the tabular clamped against the cell rule while
            # \arraystretch{1.6} keeps prose cells one stretched baseline
            # lower — the row-internal asymmetry the user flagged.
            # (?!\[) avoids double-injection if the AI already emitted an
            # explicit alignment option. Inner tabulars get [t] too so their
            # top row aligns with surrounding prose in the outer cell.
            block = re.sub(r"\\begin\{(tabular\*?)\}(?!\[)",
                           r"\\begin{\1}[t]", block, count=1)
            if idx in top_level:
                block = "\\adjustbox{max width=\\linewidth}{" + block + "}"
                sep = "{\\par\\addvspace{" + _TABULAR_VSPACE + "}}"
                return sep + block + sep
        return block

    # Outer expansion exposes inner placeholders that re.sub won't re-scan.
    # Cap mirrors the stash-side defensive cap; real depth is 2-3.
    for _ in range(10):
        if not _ENV_PLACEHOLDER_RE.search(text):
            break
        text = _ENV_PLACEHOLDER_RE.sub(_restore, text)
    # Wrapper provides the paragraph break + vspace; surrounding AI-emitted
    # ``\newline`` is now redundant. Match against the wrapper's literal
    # text (not ``\begin{tabular}``) since the wrapper sits between any
    # preceding ``\newline`` and the env start.
    _wrapper_pat = re.escape("{\\par\\addvspace{" + _TABULAR_VSPACE + "}}")
    text = re.sub(r"\\newline\s+(?=" + _wrapper_pat + ")", "", text)
    text = re.sub("(" + _wrapper_pat + r")\s*\\newline\b\s?", r"\1 ", text)
    # A wrapper at the very start of the cell text means the tabular is the
    # first content in its longtable cell. The wrapper's leading \par then
    # closes the cell's initial empty paragraph and manufactures a full
    # baseline of vertical space above the table — visible as a free line in
    # the PDF. Mid-text wrappers (after-prose, before-prose) still need the
    # \par to enter vertical mode, so leave those alone. The preceding
    # ``\newline``-strip already ate any AI-emitted leading newline before
    # the wrapper, so this anchor fires iff the tabular is genuinely at
    # cell start.
    text = re.sub(r"^\s*" + _wrapper_pat, "", text)
    return text


def _convert_literal_bullets(t: str) -> str:
    """Wrap runs of >=2 lines starting with ``- `` or ``• `` in an itemize
    block. The marking model occasionally forgets the FIELD_RULES rule and
    emits literal markers joined by ``\\newline``; this safety net rebuilds
    the intended list before the cell is rendered.

    A singleton (one such line on its own) has its marker stripped and
    becomes plain prose — wrapping a single line in itemize adds visual
    noise without conveying list structure.
    """
    lines = _LINE_BREAK_RE.split(t)
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _BULLET_LINE_RE.match(lines[i])
        if m:
            items = [m.group(1)]
            j = i + 1
            while j < len(lines):
                m2 = _BULLET_LINE_RE.match(lines[j])
                if not m2:
                    break
                items.append(m2.group(1))
                j += 1
            if len(items) >= 2:
                # Match existing AI-emitted itemize style: space after
                # \begin{itemize} and before each \item — keeps .tex diffs
                # tidy when comparing helper output against model output.
                out.append(
                    r"\begin{itemize} "
                    + " ".join(rf"\item {b}" for b in items)
                    + r" \end{itemize}"
                )
            else:
                out.append(items[0])  # singleton: drop marker, keep content
            i = j
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


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
        t = re.sub(r"(?:\\newline\s*)+(?=\\begin\{)", "", t)
        t = re.sub(r"(?<=\})(?:\\newline\s*)+(?=\\begin\{)", "", t)
        t = re.sub(r"(\\begin\{[^}]+\})(?:\s*\\newline\b\s?)+", r"\1", t)
        t = re.sub(r"(\\end\{[^}]+\})(?:\s*\\newline\b\s?)+", r"\1 ", t)
        t = re.sub(r"(?:\\newline\s*)+(?=\\item\b)", "", t)
        t = re.sub(r"(?:\\newline\s*)+(?=\\end\{)", "", t)
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


def _format_criteria_cell(raw: str, cell_width_cm: float = 3.6) -> str:
    """Format a marking_criteria string for the Expected column.

    Single-token criteria (one word or one number, no spaces) are grouped
    on one line joined with ' / '. Multi-word criteria each get their own line.

    ``\\begin{alltt}…\\end{alltt}`` blocks are stashed before the strip-and-group
    pass so their internal indentation survives — otherwise ``line.strip()``
    would eat the leading whitespace on every code line.

    *cell_width_cm* is forwarded to `_ai_cell` for alltt font-size selection.
    """
    stashed: list[str] = []

    def _stash(m: re.Match) -> str:
        stashed.append(m.group(0))
        return f"\x00ALLTT{len(stashed) - 1}\x00"

    text = _ALLTT_BLOCK_RE.sub(_stash, raw)

    lines = []
    for line in text.split("\n"):
        line = re.sub(r"^\s*\[[^\]]*\]\s*", "", line).strip()
        if line:
            lines.append(line)

    if not lines:
        return "---"

    segments: list[str] = []
    short_group: list[str] = []
    for criterion in lines:
        # single token: one word or one number (not a LaTeX command, not a stashed alltt placeholder)
        is_short = (
            " " not in criterion
            and not criterion.startswith("\\")
            and not criterion.startswith("\x00")
        )
        if is_short:
            short_group.append(criterion)
        else:
            if short_group:
                segments.append(" / ".join(short_group))
                short_group = []
            segments.append(criterion)
    if short_group:
        segments.append(" / ".join(short_group))

    result = "\n".join(segments)
    result = _ALLTT_PLACEHOLDER_RE.sub(
        lambda m: stashed[int(m.group(1))], result
    )
    return _ai_cell(result, cell_width_cm)


# Per-orientation panel budgets used by `_split_oversized_cell` to detect
# Expected cells whose formatted height would exceed a single longtable row
# (Q12-style mega mark schemes). Units are "prose-line equivalents":
# 10 pt × \arraystretch{1.6} ≈ 16 pt vertical per prose line; alltt lines
# weigh less per the size command (see _ALLTT_SIZE_WEIGHTS below).
# Landscape A4 has ~16 cm of vertical text → ~22 prose lines with margin.
# Portrait A4 is ~28 cm tall → ~40.

_ALLTT_SIZE_WEIGHTS: tuple[tuple[str, float], ...] = (
    (r"\tiny ", 0.4),
    (r"\scriptsize ", 0.55),
    (r"\footnotesize ", 0.7),
)
_ALLTT_HEADER_RE = re.compile(
    r"\\begin\{alltt\}((?:\\(?:tiny|scriptsize|footnotesize|small|normalsize)\s)?)"
)
_ALLTT_KEYWORD_RE = re.compile(r"^\s*(?:PROCEDURE|FUNCTION|SUBROUTINE)\b")


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


def _sub_split_alltt_block(block: str) -> list[str]:
    """Sub-split an oversized alltt block at blank lines (preferred) or
    PROCEDURE/FUNCTION/SUBROUTINE keyword boundaries; preserve the parent's
    size command verbatim on each sub-block."""
    h = _ALLTT_HEADER_RE.match(block)
    if not h or not block.endswith(r"\end{alltt}"):
        return [block]
    size_prefix = h.group(1)
    body = block[h.end() : -len(r"\end{alltt}")]
    lines = body.split("\n")

    groups: list[list[str]] = [[]]
    for ln in lines:
        if ln.strip() == "" and groups[-1]:
            groups.append([])
        else:
            groups[-1].append(ln)
    groups = [g for g in groups if g]
    if len(groups) < 2:
        groups = [[]]
        for ln in lines:
            if _ALLTT_KEYWORD_RE.match(ln) and groups[-1]:
                groups.append([])
            groups[-1].append(ln)
        groups = [g for g in groups if g]
    if len(groups) < 2:
        return [block]
    return [
        r"\begin{alltt}" + size_prefix + "\n".join(g) + r"\end{alltt}"
        for g in groups
    ]


def _decompose_cell(cell: str, budget: float):
    pos = 0
    for m in _ALLTT_BLOCK_RE.finditer(cell):
        if pos < m.start():
            yield from _split_prose_lines(cell[pos : m.start()])
        block = m.group(0)
        if _chunk_weight(block) > budget:
            yield from _sub_split_alltt_block(block)
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


# ---------------------------------------------------------------------------
# Jinja2 environment — non-default delimiters to avoid LaTeX brace clashes.
# ``keep_trailing_newline=True`` preserves the trailing newline of each
# template file so output matches the original f-string serializers exactly.
# ---------------------------------------------------------------------------

_ENV = jinja2.Environment(
    block_start_string="<%",   block_end_string="%>",
    variable_start_string="<<", variable_end_string=">>",
    comment_start_string="<#", comment_end_string="#>",
    loader=jinja2.FileSystemLoader(Path(__file__).parent / "templates"),
    keep_trailing_newline=True,
    autoescape=False,
)


def _student_header_kwargs(
    report: dict,
    exam_name: str,
    show_curved_grade: bool,
    class_avg: int | None,
) -> dict:
    """Build the Jinja2 kwargs shared by all three per-student-report templates."""
    pct = report["percentage"]
    curved_pct = report.get("curved_pct")
    pct_display = "N/A" if pct is None else f"{pct}\\%"
    curved_display = "N/A" if curved_pct is None else f"{curved_pct}\\%"
    return {
        "name": _latex_escape(report["student_name"]),
        "header_extra": f" — {_latex_escape(exam_name.replace('_', ' '))}" if exam_name else "",
        "total": report["total_marks"],
        "max_m": report["max_marks"],
        "percentage": pct,
        "class_avg_pct": class_avg,
        "summary_text": (
            f"{pct_display} raw, {curved_display} curved"
            if show_curved_grade else pct_display
        ),
        "date_str": datetime.date.today().isoformat(),
    }


def _student_report_to_tex(
    report: dict,
    exam_name: str = "",
    orientation: str = "landscape",
    font_size: int = 10,
    show_curved_grade: bool = True,
    class_avg: int | None = None,
) -> str:
    # Column widths threaded into _ai_cell / _format_criteria_cell so alltt
    # font-size selection scales with cell width. Match the col_spec below.
    # `panel_budget` is forwarded to `_split_oversized_cell` so very tall
    # Expected cells are emitted as multi-row panels (avoids cell overflow
    # off-page; longtable allows page breaks between rows but not within).
    if orientation == "portrait":
        ans_w, exp_w, reason_w = 3.6, 5.0, 5.5
        panel_budget = 40.0
    else:
        ans_w, exp_w, reason_w = 5.7, 7.0, 8.1
        panel_budget = 22.0
    rows = []
    for q in report["questions"]:
        qnum = _latex_escape(str(q.get("number", "")).replace("_", "."))
        max_q = q.get("max_marks", "")
        awarded = q.get("assigned_marks")
        answer_raw = str(q.get("student_answer") or "").strip()
        if q.get("_unanswered"):
            answer = "\\textit{(not answered)}"
        elif not answer_raw:
            answer = "\\textit{(blank)}"
        else:
            answer = _ai_cell(answer_raw, ans_w)
        correct_raw = str(q.get("correct_answer") or "").strip()
        criteria_raw = str(q.get("marking_criteria") or "").strip()
        question_type = str(q.get("question_type", "")).strip()
        if question_type == "multiple_choice" or not criteria_raw:
            # MCQ: always show the answer letter.
            # Non-MCQ without criteria: fall back to correct_answer.
            correct_ans = _ai_cell(correct_raw, exp_w) if correct_raw else "---"
        else:
            # Non-MCQ with criteria: show the full breakdown regardless of correct_answer.
            correct_ans = _format_criteria_cell(criteria_raw, exp_w)
        reasoning = _ai_cell(str(q.get("explanation") or ""), reason_w)
        awarded_cell = _awarded_tex(awarded, max_q)
        panels = _split_oversized_cell(correct_ans, panel_budget)
        if len(panels) == 1:
            rows.append(
                f"    {qnum} & {max_q} & {awarded_cell} & {answer} & {panels[0]} & {reasoning} \\\\ \\hline"
            )
        else:
            # Q12-style mega Expected: emit one row per panel; continuation
            # rows have empty Q/Max/Got/Answer/Reasoning so panels appear as
            # one logical row. Only the final continuation row carries
            # \hline (separates from the next question).
            rows.append(
                f"    {qnum} & {max_q} & {awarded_cell} & {answer} & {panels[0]} & {reasoning} \\\\"
            )
            for i, panel in enumerate(panels[1:], 1):
                terminator = "\\\\ \\hline" if i == len(panels) - 1 else "\\\\"
                rows.append(f"     &  &  &  & {panel} &  {terminator}")
    rows_str = "\n".join(rows)
    # Column widths fill the available text width minus ~2.5 cm of
    # \tabcolsep separator overhead across 6 columns:
    #   landscape A4 (25.7 cm text - 2.5 cm overhead) → ~22.7 cm column budget
    #     = p{0.6} + p{0.6} + p{0.7} + p{5.7} + p{7.0} + p{8.1}
    #   portrait  A4 (19.0 cm text - 2.5 cm overhead) → ~16.5 cm column budget
    #     = p{0.4} + p{0.4} + p{0.5} + p{3.6} + p{5.0} + p{5.5}
    # Portrait widths × (16.5 / 22.7) ≈ 0.727 of landscape widths.
    if orientation == "portrait":
        geometry_line = "\\geometry{a4paper,margin=1cm,footskip=6pt}\n"
        col_spec = "L{0.4cm}L{0.4cm}L{0.5cm}L{3.6cm}L{5cm}L{5.5cm}"
    else:
        geometry_line = "\\geometry{a4paper,landscape,margin=2cm}\n"
        col_spec = "L{0.6cm}L{0.6cm}L{0.7cm}L{5.7cm}L{7.0cm}L{8.1cm}"
    table_open  = "{\\small\n" if font_size < 12 else ""
    table_close = "}\n"        if font_size < 12 else ""
    return _ENV.get_template("student_report.tex.j2").render(
        font_size=font_size,
        geometry_line=geometry_line,
        table_open=table_open,
        col_spec=col_spec,
        rows_str=rows_str,
        table_close=table_close,
        **_student_header_kwargs(report, exam_name, show_curved_grade, class_avg),
    )


def _format_q_rows(avgs: dict, q_max: dict, q_pct: dict) -> str:
    """Build LaTeX rows for a question-ranking table, sorted hardest first."""
    rows = []
    for qnum, avg in sorted(
        avgs.items(),
        key=lambda x: (q_pct.get(x[0], float("inf")), x[0]),
    ):
        max_cell = str(q_max.get(qnum, "")) if q_max else ""
        pct_cell = f"{q_pct[qnum]}\\%" if qnum in q_pct else "N/A"
        rows.append(
            f"    {_latex_escape(qnum.replace('_', '.'))} & {max_cell} & {avg} & {pct_cell} \\\\"
        )
    return "\n".join(rows)


def _class_report_to_tex(report: dict, exam_name: str = "") -> str:
    header_extra = f" — {_latex_escape(exam_name.replace('_', ' '))}" if exam_name else ""
    date_str = datetime.date.today().isoformat()
    student_rows = []
    for s in report["students"]:
        name = _latex_escape(s["name"])
        pct_display = "N/A" if s["percentage"] is None else f"{s['percentage']}\\%"
        curved_display = "N/A" if s.get("curved_pct") is None else f"{s['curved_pct']}\\%"
        rank_cell = str(s["rank"]) if s.get("rank") is not None else "---"
        student_rows.append(f"    {rank_cell} & {name} & {s['total_marks']} & {pct_display} & {curved_display} \\\\")
    student_rows_str = "\n".join(student_rows)

    q_max = report.get("per_question_max_marks", {})
    q_pct = report.get("per_question_pct_averages", {})
    q_rows_str = _format_q_rows(report.get("per_question_averages", {}), q_max, q_pct)

    top_q_rows_str = _format_q_rows(
        report.get("per_top_question_averages", {}),
        report.get("per_top_question_max_marks", {}),
        report.get("per_top_question_pct_averages", {}),
    )

    class_avg_display = (
        "N/A" if report["class_average_pct"] is None
        else f"{report['class_average_pct']}\\%"
    )

    return _ENV.get_template("class_report.tex.j2").render(
        header_extra=header_extra,
        class_avg_display=class_avg_display,
        total_max_marks=report["total_max_marks"],
        n_students=report.get("n_students"),
        median_pct=report.get("median_pct"),
        min_pct=report.get("min_pct"),
        max_pct=report.get("max_pct"),
        date_str=date_str,
        student_rows_str=student_rows_str,
        q_rows_str=q_rows_str,
        top_q_rows_str=top_q_rows_str,
        histogram_raw_path=report.get("histogram_raw_path"),
        histogram_curved_path=report.get("histogram_curved_path"),
        difficulty_path=report.get("difficulty_path"),
    )


# ---------------------------------------------------------------------------
# Parsed-exam question rendering (step 28: exam_questions.pdf,
# *_landscape_with_questions.pdf, *_portrait_list.pdf).
# ---------------------------------------------------------------------------

def _build_question_index(parsed_questions: list[dict]) -> dict[str, dict]:
    """DFS the parsed-exam tree; map every node's bare number to its dict."""
    index: dict[str, dict] = {}

    def _visit(q: dict) -> None:
        num = str(q.get("number", "")).strip()
        if num:
            index[num] = q
        for sub in q.get("subquestions") or []:
            _visit(sub)

    for q in parsed_questions or []:
        _visit(q)
    return index


def _question_text_for_row(qnum: str, qmap: dict[str, dict]) -> dict | None:
    """Look up a parsed-exam question by row number, stripping ``_2``-style suffix."""
    base = str(qnum).partition("_")[0]
    return qmap.get(base)


def _render_question_text(q: dict | None, cell_width_cm: float = 3.6) -> str:
    """Render only the question stem — for the narrow Question column in the
    landscape with-questions table."""
    if not q:
        return r"\textit{(text unavailable)}"
    text = str(q.get("text") or "").strip()
    if not text:
        return r"\textit{(no stem)}"
    return _ai_cell(text, cell_width_cm)


def _render_question_with_options(q: dict | None, cell_width_cm: float = 3.6) -> str:
    """Render question stem plus an ``(A)/(B)/...`` itemize for MCQs.

    Returns empty string for parent-only nodes (text == "" and no options) so
    the recursive ``_question_to_tex`` can omit a blank body line for them.
    """
    if not q:
        return r"\textit{(text unavailable)}"
    parts: list[str] = []
    text = str(q.get("text") or "").strip()
    if text:
        parts.append(_ai_cell(text, cell_width_cm))
    if str(q.get("type") or "") == "multiple_choice":
        opts = q.get("answer_options") or []
        if opts:
            items: list[str] = []
            for opt in opts:
                letter = _latex_escape(str(opt.get("letter") or "").strip())
                opt_text = _ai_cell(str(opt.get("text") or "").strip(), cell_width_cm)
                items.append(f"  \\item[({letter})] {opt_text}")
            parts.append(
                "\\begin{itemize}[leftmargin=2em,itemsep=0pt]\n"
                + "\n".join(items)
                + "\n\\end{itemize}"
            )
    return "\n".join(parts)


def _question_to_tex(q: dict, depth: int = 0) -> str:
    """Recursive renderer used by ``exam_questions.pdf``.

    Top-level questions render flush-left; subquestions are indented with
    ``\\setlength{\\leftskip}{...em}`` inside a TeX group so wrapped lines stay
    aligned without pulling in ``changepage``.
    """
    num = _latex_escape(str(q.get("number", "")))
    marks = q.get("marks") or 0
    marks_label = ""
    if marks:
        marks_label = f" \\hfill {marks} mark{'s' if marks != 1 else ''}"
    # exam_questions.pdf is A4 with 1.5cm margins → 18cm text width. Each
    # subquestion depth indents 1.5em (~0.5cm at 11pt body); subtract that
    # so deeply-nested alltt sizes correctly. Floor at 4cm to avoid silly
    # widths if the recursion ever goes very deep.
    block_w = max(4.0, 18.0 - depth * 0.5)
    body = _render_question_with_options(q, block_w)

    lines = [f"\\noindent\\textbf{{Q{num}}}{marks_label}\\par\\nopagebreak"]
    if body:
        lines.append(f"{body}\\par")
    block = "\n".join(lines)
    if depth > 0:
        block = f"{{\\setlength{{\\leftskip}}{{{depth * 1.5}em}}\n{block}\n}}"

    subs = q.get("subquestions") or []
    if subs:
        sub_blocks = "\n\\smallskip\n".join(
            _question_to_tex(sub, depth + 1) for sub in subs
        )
        block = f"{block}\n\\smallskip\n{sub_blocks}"
    return block


def _exam_questions_to_tex(parsed_questions: list[dict], exam_name: str = "") -> str:
    """Render all parsed exam questions into a standalone TeX document."""
    header_extra = f" — {_latex_escape(exam_name.replace('_', ' '))}" if exam_name else ""
    date_str = datetime.date.today().isoformat()
    body_blocks = [_question_to_tex(q) for q in parsed_questions or []]
    body = "\n\\vspace{1em}\n".join(body_blocks)
    return _ENV.get_template("exam_questions.tex.j2").render(
        header_extra=header_extra,
        date_str=date_str,
        body=body,
    )


def _student_report_with_questions_to_tex(
    report: dict,
    qmap: dict[str, dict],
    exam_name: str = "",
    font_size: int = 10,
    show_curved_grade: bool = True,
    class_avg: int | None = None,
) -> str:
    """Landscape per-student PDF with an extra Question column (no MCQ options)."""
    # Column widths threaded into _ai_cell / _format_criteria_cell /
    # _render_question_text so alltt font-size selection scales with cell
    # width. Match the col_spec below.
    qstem_w, ans_w, exp_w, reason_w = 4.5, 4.7, 5.0, 6.2
    rows = []
    for q in report["questions"]:
        qnum_raw = str(q.get("number", ""))
        qnum = _latex_escape(qnum_raw.replace("_", "."))
        max_q = q.get("max_marks", "")
        awarded = q.get("assigned_marks")
        answer_raw = str(q.get("student_answer") or "").strip()
        if q.get("_unanswered"):
            answer = "\\textit{(not answered)}"
        elif not answer_raw:
            answer = "\\textit{(blank)}"
        else:
            answer = _ai_cell(answer_raw, ans_w)
        correct_raw = str(q.get("correct_answer") or "").strip()
        criteria_raw = str(q.get("marking_criteria") or "").strip()
        question_type = str(q.get("question_type", "")).strip()
        if question_type == "multiple_choice" or not criteria_raw:
            correct_ans = _ai_cell(correct_raw, exp_w) if correct_raw else "---"
        else:
            correct_ans = _format_criteria_cell(criteria_raw, exp_w)
        reasoning = _ai_cell(str(q.get("explanation") or ""), reason_w)
        awarded_cell = _awarded_tex(awarded, max_q)
        question_cell = _render_question_text(_question_text_for_row(qnum_raw, qmap), qstem_w)
        rows.append(
            f"    {qnum} & {question_cell} & {max_q} & {awarded_cell} & {answer} & {correct_ans} & {reasoning} \\\\ \\hline"
        )
    rows_str = "\n".join(rows)
    # Landscape A4: 25.7 cm text - ~3.0 cm \tabcolsep overhead across 7 cols
    # → ~22.7 cm column budget = 0.5+4.5+0.5+0.6+4.7+5.0+6.2 (cm).
    geometry_line = "\\geometry{a4paper,landscape,margin=2cm}\n"
    col_spec = "L{0.5cm}L{4.5cm}L{0.5cm}L{0.6cm}L{4.7cm}L{5.0cm}L{6.2cm}"
    table_open  = "{\\small\n" if font_size < 12 else ""
    table_close = "}\n"        if font_size < 12 else ""
    return _ENV.get_template("student_report_with_questions.tex.j2").render(
        font_size=font_size,
        geometry_line=geometry_line,
        table_open=table_open,
        col_spec=col_spec,
        rows_str=rows_str,
        table_close=table_close,
        **_student_header_kwargs(report, exam_name, show_curved_grade, class_avg),
    )


def _student_report_list_to_tex(
    report: dict,
    qmap: dict[str, dict],
    exam_name: str = "",
    show_curved_grade: bool = True,
    class_avg: int | None = None,
) -> str:
    """Portrait per-student PDF in a list/block layout (no longtable).

    Each row of ``report["questions"]`` becomes one block: header line, question
    prompt (with MCQ options inline), then labeled paragraphs for student
    answer / expected / reasoning, separated by a thin horizontal rule.
    """
    # Block layout, no longtable: each labeled paragraph spans the full text
    # width. A4 portrait with 1.5cm margins = 21 - 3 = 18cm.
    block_w = 18.0

    blocks: list[str] = []
    for q in report["questions"]:
        qnum_raw = str(q.get("number", ""))
        qnum_dotted = _latex_escape(qnum_raw.replace("_", "."))
        max_q = q.get("max_marks", "")
        awarded = q.get("assigned_marks")
        awarded_cell = _awarded_tex(awarded, max_q)
        answer_raw = str(q.get("student_answer") or "").strip()
        if q.get("_unanswered"):
            answer = "\\textit{(not answered)}"
        elif not answer_raw:
            answer = "\\textit{(blank)}"
        else:
            answer = _ai_cell(answer_raw, block_w)
        correct_raw = str(q.get("correct_answer") or "").strip()
        criteria_raw = str(q.get("marking_criteria") or "").strip()
        question_type = str(q.get("question_type", "")).strip()
        if question_type == "multiple_choice" or not criteria_raw:
            expected = _ai_cell(correct_raw, block_w) if correct_raw else "---"
        else:
            expected = _format_criteria_cell(criteria_raw, block_w)
        reasoning = _ai_cell(str(q.get("explanation") or ""), block_w)
        question_body = _render_question_with_options(
            _question_text_for_row(qnum_raw, qmap), block_w
        ) or r"\textit{(text unavailable)}"
        blocks.append(
            f"\\noindent\\textbf{{Q{qnum_dotted}}} \\hfill {awarded_cell} / {max_q}\\par\n"
            f"\\smallskip\\textbf{{Question:}}\\par\n"
            f"{question_body}\\par\n"
            f"\\smallskip\\textbf{{Your answer:}}\\par\n"
            f"{answer}\\par\n"
            f"\\smallskip\\textbf{{Expected:}}\\par\n"
            f"{expected}\\par\n"
            f"\\smallskip\\textbf{{Reasoning:}}\\par\n"
            f"{reasoning}\\par\n"
            f"\\vspace{{0.4em}}\\hrule\\vspace{{0.6em}}"
        )
    body = "\n".join(blocks)

    return _ENV.get_template("student_report_list.tex.j2").render(
        body=body,
        **_student_header_kwargs(report, exam_name, show_curved_grade, class_avg),
    )
