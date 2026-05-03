---
name: latex_yaml_style
version: v1
description: Shared LaTeX-in-YAML style guide. Inlined into prompts via `$include_latex_yaml_style` (resolved by xscore.prompts.loader). Covers the "never double-quote" rule, single-quote vs block-scalar choice, LaTeX commands inside block scalars, alltt for code, and common math syntax. Used by steps 20, 24, 28, 29.
---
## YAML quoting

YAML scalar quoting matters because text routinely contains LaTeX backslashes — and a single wrong quote silently destroys them.

**Never use double quotes for any non-empty string field.** Double quotes interpret `\` as an escape introducer, so `"\texttt{DIV}"` parses to a literal TAB followed by `exttt{DIV}` — silently destroying the LaTeX command. (Empty `""` is fine — there's no `\` to misinterpret.)

When to use which form:

- Plain short value with no special characters → no quoting: `text: A particle moves in a circle`, `letter: A`.
- Single-line value containing a backslash (LaTeX commands like `\texttt{DIV}`, `\leftarrow`) → single quotes: `text: '\texttt{DIV}'`. Single quotes do not interpret escapes; the backslash is preserved literally.
- Single-line value containing both a single quote and a backslash, or any multi-line value → block scalar (`|`):

      text: |
        First line with a \texttt{token}
        Second line

WRONG: `text: "\texttt{DIV}"`     ← becomes `<TAB>exttt{DIV}` on parse
RIGHT: `text: '\texttt{DIV}'`     ← preserves `\texttt{DIV}`

## LaTeX commands inside block scalars

Block scalars (`|`) handle backslashes literally — write LaTeX commands directly without escaping:

- bold text → `\textbf{...}`
- italic text → `\textit{...}`
- unordered lists → `\begin{itemize}\item first\item second\end{itemize}`
- ordered/numbered lists → `\begin{enumerate}\item first\item second\end{enumerate}`
- tables → `\begin{tabular}{col-spec} cell & cell \\ next row \end{tabular}` with `\hline` between rows
- inline math → `$...$`; display math → `$$$$...$$$$`
- explicit line breaks between prose sentences → `\newline`

Constraints:
- Never use `\newline` immediately after `\begin{...}` or before `\end{...}`.
- Never use more than one `\newline` in a row.
- List items begin directly with `\item` — no `\newline` between items.
- Plain prose and introductory sentences are written verbatim (no wrapping command needed).

## Code and pseudocode (alltt)

Wrap multi-line code or pseudocode in `\begin{alltt}...\end{alltt}`; preserve indentation with literal spaces; use real newlines between lines.

Inside `\begin{alltt}...\end{alltt}`: do NOT escape `<`, `>`, `&`, `%`, `_`, `#`, `$` — alltt is verbatim-with-commands. Only escape `{` → `\{`, `}` → `\}`, backslash → `\textbackslash{}`.

Wrap inline code tokens (variable names, function calls, single keywords like `IF` / `WHILE` / `DECLARE` / `RETURN`) in `\texttt{...}`.

NEVER use `\textbf{...}` for code — bold is not monospace. Save `\textbf{...}` for emphasis on prose words.
