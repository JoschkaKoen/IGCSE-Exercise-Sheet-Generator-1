"""LaTeX-source text manipulation primitives used by the report renderer.

Pure text-in / text-out helpers: character escaping, math-mode wrapping,
``alltt`` font-size selection and line-breaking, bullet-line conversion,
and the ``\\begin{...}\\end{...}`` env-protection passes used to keep
``tabular`` and ``alltt`` blocks intact while the surrounding prose is
munged.

No Jinja, no template state — these functions can be unit-tested with raw
strings. ``report_latex_cells.py`` composes them into AI-cell formatters,
and ``report_latex.py`` renders the final reports.
"""

from __future__ import annotations

import math
import re


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
# cell. Empirical thresholds (20 / 32 chars at the 3.6cm baseline, scaled by
# `cell_width_cm / 3.6`):
#   - body 10pt: fits up to ~20 chars before overflow
#   - \footnotesize: up to ~32 chars
#   - \scriptsize: anything longer; horizontally wrapped to ~32 chars by
#     `_wrap_alltt_at_spaces` (post-pass in `_protect_alltt._restore`).
# Scriptsize is the readability floor — `\tiny` is no longer selected. The
# old `\tiny` branch was retired because (a) tiny is hard to read and (b)
# even at tiny, lines longer than ~55 chars at 3.6cm still overflowed the
# cell visually. The new contract: pick the largest size whose threshold
# fits the longest input line; everything above the foot threshold goes to
# scriptsize and gets wrapped.
#
# Empirical observation backing the 32-char scriptsize budget: in Cosmo's
# Q10 landscape report (5.7cm cell), `Average <- Total / 24, Average <-
# Round(Average, -2)` (52 chars) overflows at scriptsize, so the 5.7cm
# scriptsize budget is ≤51 → 32 at 3.6cm via the same scaling.
def _alltt_size_command(block: str, cell_width_cm: float = 3.6) -> str:
    inner = re.sub(r"\\(?:begin|end)\{alltt\}", "", block)
    inner = inner.replace(r"\textbackslash{}", "\\")  # 16-char escape → 1
    max_len = max((len(ln) for ln in inner.split("\n")), default=0)
    scale = cell_width_cm / 3.6
    if max_len <= math.ceil(20 * scale):
        return ""
    if max_len <= math.ceil(32 * scale):
        return "\\footnotesize "
    # Anything wider than the foot threshold goes to scriptsize and is
    # horizontally wrapped by `_wrap_alltt_at_spaces`. We do not go below
    # scriptsize — tiny is below the readability floor.
    return "\\scriptsize "


# Char budget for the size returned by `_alltt_size_command`, scaled by
# cell width. Used as the wrap target by `_wrap_alltt_at_spaces`. For
# body/foot the budget equals the selection threshold (so wrap is a no-op
# — selection guarantees the input already fits). For scriptsize the
# budget is also 32 chars at 3.6cm: scriptsize is the catch-all for
# overflowing input, so wrapping reduces every line back into the cell.
_ALLTT_SCRIPT_BUDGET_BASE = 32  # chars at 3.6 cm; empirical from rendered scriptsize


def _alltt_budget_for_size(size_cmd: str, cell_width_cm: float) -> int:
    """Char budget for the size returned by `_alltt_size_command`."""
    scale = cell_width_cm / 3.6
    if size_cmd == "":
        return math.ceil(20 * scale)
    if "footnotesize" in size_cmd:
        return math.ceil(32 * scale)
    return math.ceil(_ALLTT_SCRIPT_BUDGET_BASE * scale)  # scriptsize


def _effective_len(s: str) -> int:
    """Length of *s* counting `\\textbackslash{}` (16 raw chars, 1 rendered)
    as one effective char. Other LaTeX escapes are not collapsed because
    inside alltt only `\\textbackslash{}` is regularly emitted by upstream
    AI prompts; bare `&`, `%`, `<`, `>` etc. pass through verbatim."""
    return len(s.replace(r"\textbackslash{}", "\\"))


def _find_last_space_within(s: str, budget: int) -> int | None:
    """Position in *s* of the last space whose effective char index is
    <= *budget*, or None if no space falls within budget. The cursor walks
    forward; a `\\textbackslash{}` escape is consumed as one effective
    char (16 raw)."""
    pos, eff, last_space = 0, 0, None
    while pos < len(s) and eff <= budget:
        if s.startswith(r"\textbackslash{}", pos):
            eff += 1
            pos += 16
            continue
        if s[pos] == " ":
            last_space = pos
        eff += 1
        pos += 1
    return last_space


def _wrap_alltt_at_spaces(block: str, budget: int) -> str:
    """Wrap each line of the alltt body at the last space at-or-before
    *budget* effective chars. Continuation indent is the original line's
    leading whitespace + 2 spaces, matching the AND/OR break style used
    by `_break_alltt_long_lines`. Lines whose first overflow has no space
    within budget (single token too long) pass through unchanged — the
    user's rule is "spaces only, not inside words"."""
    m = re.match(r"(\\begin\{alltt\})(.*)(\\end\{alltt\})", block, re.DOTALL)
    if not m:
        return block
    prefix, body, suffix = m.groups()
    out: list[str] = []
    for line in body.split("\n"):
        rest = line
        leading = re.match(r"^\s*", line).group(0)
        cont_indent = leading + "  "
        while _effective_len(rest) > budget:
            cut = _find_last_space_within(rest, budget)
            if cut is None:
                break  # single token too long — accept overflow on this line
            out.append(rest[:cut].rstrip())
            rest = cont_indent + rest[cut:].lstrip()
        out.append(rest)
    return prefix + "\n".join(out) + suffix


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
        \\[#_&%${{}}~^\\]           # escaped LaTeX special — consume `\_` etc. as one unit
      | \\[A-Za-z]+(?:{_BRACE})*    # \cmd{{args}} (text-mode ones already stashed)
      | [\^_]{_BRACE}               # ^{{x}} or _{{x}}
      | [\^_][A-Za-z0-9]            # ^x or _x (single char)
      | {_BRACE}                    # bare brace group
      | [A-Za-z0-9+\-*/=().,]       # alphanum / operators
    )+
    """,
    re.VERBOSE,
)
_MATH_INDICATOR_RE = re.compile(rf"(?<!\\)[\^_]|\\(?:{_MATH_CMDS})\b")
_STASH_RE = re.compile(r"\x00TXT(\d+)\x00")

# Math-region forms beyond the inline ``$…$`` that ``_DOLLAR_SPLIT_RE`` already
# handles. Stashed before the main heuristic runs so it operates only on prose,
# not on math regions the AI already wrapped with an alternative syntax.
# Without this, valid AI output like ``$$X = (A \text{ OR } B)$$`` gets shredded
# (Q8 in run 2026-05-04_10-34-59 demonstrated the failure mode: the leading
# ``$$`` matched ``$…$`` as empty inline math, the middle was treated as text,
# and ``_MATH_RUN_RE`` then wrapped each ``\text{…}`` in fresh ``$…$``).
# ``\(…\)`` and ``\[…\]`` are LaTeX-canonical alternatives the AI also emits.
_OTHER_MATH_RE = re.compile(
    r"\$\$.*?\$\$"      # $$…$$ display (TeX)
    r"|\\\(.*?\\\)"     # \(…\) inline (LaTeX)
    r"|\\\[.*?\\\]",    # \[…\] display (LaTeX)
    re.DOTALL,
)
_OTHER_MATH_PLACEHOLDER_RE = re.compile(r"\x00MATH(\d+)\x00")


def _wrap_loose_math(text: str) -> str:
    other_math: list[str] = []

    def _stash_other(m: re.Match) -> str:
        other_math.append(m.group(0))
        return f"\x00MATH{len(other_math) - 1}\x00"

    text = _OTHER_MATH_RE.sub(_stash_other, text)

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
    text = _STASH_RE.sub(lambda m: stashed[int(m.group(1))], text)
    return _OTHER_MATH_PLACEHOLDER_RE.sub(
        lambda m: other_math[int(m.group(1))], text
    )


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
        # Post-size-selection wrap: any line still exceeding the chosen
        # size's char budget is broken at the last space at-or-before
        # budget. For body / footnote this is a no-op (selection
        # guarantees max_line ≤ budget); for scriptsize (the catch-all
        # for overflowing input) it cuts long lines down to size.
        block = _wrap_alltt_at_spaces(
            block, _alltt_budget_for_size(size, cell_width_cm))
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
    text = re.sub(r"(?:\\newline\s*)+(?=" + _wrapper_pat + ")", "", text)
    text = re.sub("(" + _wrapper_pat + r")(?:\s*\\newline\b\s?)+", r"\1 ", text)
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
