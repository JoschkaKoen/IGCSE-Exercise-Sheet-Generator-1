---
name: shared_latex_rules
version: v4
description: Shared LaTeX-in-YAML style guide. Inlined into prompts via `$include_shared_latex_rules` (resolved by xscore.prompts.loader). Covers YAML quoting (split into free-text vs structural fields), LaTeX commands inside block scalars, alltt for code, and math wrapping. Used by steps 20, 24, 28, 29. v4 expanded the math rule from a one-liner to a full section with physics/chemistry/mixed-text examples after the s23_22 run found `$$X = (A \text{ OR } B)$$` corruption (the renderer's `_wrap_loose_math` heuristic was the proximate cause and got fixed in tandem, but the prompt was doing zero teaching). Also strengthened the alltt opener from "code or pseudocode" to "any multi-line code or programming-language answer" with an explicit language enumeration, after the same run found Andy_2's Python answer transcribed without alltt because the model read "code or pseudocode" as CAIE-pseudocode-only. v3 reorganised the YAML quoting guidance into two scoped subsections — `### Free-text fields` (model-authored content, two shapes: `''` or `|`) and `### Structural fields` (verbatim-copied metadata, plain or single-quoted). Replaces v2's single decision tree that mixed both kinds and gave free-text examples like `text: A particle moves in a circle` that contradicted the new free-text rule. v2 (filename + name renamed from latex_yaml_style → shared_latex_rules; moved out of _shared/ subfolder to live alongside other prompts).
---
## YAML quoting

YAML scalar quoting matters because text routinely contains LaTeX backslashes, colons, and special characters — and a single wrong quote silently destroys them. The rules below split by who owns the field's content: model-authored free text vs. verbatim-copied structural metadata.

**Never use double quotes for any non-empty string field** (universally — applies to both kinds). Double quotes interpret `\` as an escape introducer, so `"\texttt{DIV}"` parses to a literal TAB followed by `exttt{DIV}` — silently destroying the LaTeX command. (Empty `""` or `''` is fine — there's no `\` to misinterpret; prefer `''` for consistency with the free-text rule below.)

### Free-text fields (model-authored content)

For any model-owned free-text YAML field — i.e. content the model authors itself, like `student_answer`, `correct_answer`, `text`, `explanation`, `problem`, `criterion`, option `text` — use exactly one of two shapes, never anything else:

| Case | Shape | Notes |
| --- | --- | --- |
| Empty | `field: ''` | Single-quoted empty. |
| Non-empty (anything: single-letter MCQ answer, definition, prose, calculation, multi-line, anything that could contain LaTeX or a colon) | `field: \|` block scalar | Consumes every character until dedent. Immune to colon-as-key, boolean/null tokens (`yes`/`no`/`Y`/`N`/`true`/`false`/`null`), numeric coercion, backslash escapes, embedded quotes. |

The same `|` shape applies uniformly to every non-empty value. There is no special case for MCQ letters, single safe-looking words, fixed-form labels, or any other "short" or "constrained" content — every non-empty free-text value uses `|`. Emptiness is the only thing that toggles to `''`.

### Structural fields (verbatim-copied metadata)

For fields the model copies verbatim from a prior step (question `number`, option `letter`, `type`, `marks`, integers like `assigned_marks`/`confidence`/`page`), keep the existing shape from the source — these never contain LaTeX or free-text content, so plain or single-quoted is fine:

- `number: '1a'` (single-quoted to preserve string-shape even when the value looks numeric)
- `letter: A` (plain — single-letter enum, never YAML-special since A–E aren't boolean tokens)
- `type: multiple_choice` (plain — fixed enum value)
- `marks: 3`, `assigned_marks: 2`, `confidence: 7` (bare integer)

If a structural field somehow contains a backslash (LaTeX inside a number? — should never happen, but if it does), single-quote it: `field: '\texttt{...}'`. Single quotes preserve `\` literally without the double-quote escape trap.

WRONG: `text: "\texttt{DIV}"`     ← becomes `<TAB>exttt{DIV}` on parse
RIGHT (free-text): `text: |` newline `  \texttt{DIV}`     ← block scalar preserves everything
RIGHT (structural workaround): `field: '\texttt{DIV}'`     ← single quotes preserve `\texttt{DIV}`

## LaTeX commands inside block scalars

Block scalars (`|`) handle backslashes literally — write LaTeX commands directly without escaping:

- bold text → `\textbf{...}`
- italic text → `\textit{...}`
- unordered lists → `\begin{itemize}\item first\item second\end{itemize}`
- ordered/numbered lists → `\begin{enumerate}\item first\item second\end{enumerate}`
- tables → `\begin{tabular}{col-spec} cell & cell \\ next row \end{tabular}` with `\hline` between rows
- explicit line breaks between prose sentences → `\newline`
- math → see `## Math` below

Constraints:
- Never use `\newline` immediately after `\begin{...}` or before `\end{...}`.
- Never use more than one `\newline` in a row.
- List items begin directly with `\item` — no `\newline` between items.
- Plain prose and introductory sentences are written verbatim (no wrapping command needed).

## Math

Two delimiter shapes:
- inline math → `$...$` — for formulas embedded in a sentence
- display math → `$$$$...$$$$` — for standalone equations on their own line

**Always wrap math.** Any expression containing math commands (`\frac`, `\sqrt`, `\sum`, `\int`, `\times`, `\cdot`, `\div`, `\leq`, `\geq`, `\neq`, `\approx`, `\to`, `\rightarrow`, `\leftarrow`, `\alpha`, `\beta`, `\pi`, `\rho`, `\theta`, `\sigma`, etc.), super/subscripts (`x^2`, `H_2O`, `^{12}_{6}C`), or `\text{...}` MUST be inside `$...$` or `$$...$$`. Bare math in prose crashes the PDF renderer.

**Examples — physics formulas:**
RIGHT: `Using $F = ma$ and $a = \frac{F}{m}$, we get $a = \frac{54 \text{ N}}{120 \text{ kg}} = 0.45 \text{ m/s}^2$.`
WRONG: `Using F = ma and a = \frac{F}{m}, we get a = \frac{54 \text{ N}}{120 \text{ kg}} = 0.45 \text{ m/s}^2.`

**Examples — chemistry / nuclear:**
RIGHT: `$^{212}_{86}\text{Rn} \rightarrow ^{208}_{84}\text{Po} + ^{4}_{2}\alpha$`
WRONG: `^{212}_{86}Rn \rightarrow ^{208}_{84}Po + ^{4}_{2}\alpha`

**Mixed math with text labels** — keep `\text{...}` *inside* the delimiters; never close math just to write a word and reopen it:
RIGHT: `$$X = (A \text{ OR } B) \text{ AND } C$$`
WRONG: `$$X = (A$$ \text{ OR } $$B) \text{ AND } C$$`
WRONG: `$$X = (A $\text{ OR }$ B) $\text{ AND }$ C$$`

If a single word like "OR" needs to break out of math, do it cleanly: `$A$ OR $B$`, not `$A \text{ OR } B$` followed by closing/reopening tricks.

**Display math is one block.** Inside `$$...$$`, the entire expression — variables, operators, `\text{...}` labels — stays between the two delimiter pairs. Don't insert `$...$` inline math inside `$$...$$`; the inner `$` reads as math-end and breaks the display block.

## Code and pseudocode (alltt)

Wrap **any multi-line code or programming-language answer** in `\begin{alltt}...\end{alltt}` — this includes CAIE pseudocode (`INPUT`, `OUTPUT`, `IF…ENDIF`, `FOR…NEXT`, `DECLARE`, `PROCEDURE`), Python (`def`, `for x in …`, `print()`, `#`-comments), Java/C/C++ (`public class`, `System.out.println`, `//`-comments, `{` / `}` braces), JavaScript, SQL, or any other language. The decision is "is this code?" not "is this CAIE pseudocode?". When in doubt, wrap. Preserve indentation with literal spaces; use real newlines between lines.

Inside `\begin{alltt}...\end{alltt}`: do NOT escape `<`, `>`, `&`, `%`, `_`, `#`, `$` — alltt is verbatim-with-commands. Only escape `{` → `\{`, `}` → `\}`, backslash → `\textbackslash{}`.

Wrap inline code tokens (variable names, function calls, single keywords like `IF` / `WHILE` / `DECLARE` / `RETURN`) in `\texttt{...}`.

NEVER use `\textbf{...}` for code — bold is not monospace. Save `\textbf{...}` for emphasis on prose words.
