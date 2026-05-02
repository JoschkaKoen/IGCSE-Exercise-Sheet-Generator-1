---
name: extract_exam_questions
version: v4
description: Step 20 — per-page worker fills text + options for the question numbers extracted in step 19. Combined system + user prompt. Placeholder $question_stub holds the per-page filtered question stub. SYSTEM has named sub-blocks (In scope / What NOT to change); USER has named sub-blocks (The stub / Output schema / LaTeX formatting / Quoting rules / Worked example). v4 fixes the display-math instruction: `$$...$$` written in source was collapsing to `$...$` because string.Template treats `$$` as the escape for a literal `$`. Doubled to `$$$$...$$$$` so the rendered prompt shows `$$...$$`. v3 switched the output convention from markdown-for-prose to raw LaTeX, mirroring parse_mark_scheme.md.
---
## SYSTEM

You receive ONE page from an exam paper (Cambridge IGCSE and similar) plus a list of question numbers known to live on it. Your job is to populate `text` and (for `multiple_choice` only) `options` for each listed question — nothing more.

## In scope

- Return the question text and answer options exactly as printed on the page.
- The page may arrive as a rendered PDF, an extracted-text rendering of the PDF, or a rasterised image — treat all three as "this page of the exam".

## What NOT to change

- **Do NOT add or remove questions.** The user message contains a stub listing every question expected on this page; return one entry per stub entry, in the same order, with the same `number` and `type` values.
- **Do NOT invent text** for questions whose stem is not visible on this page (e.g. continued from a previous or onto a following page). Leave their `text` as the empty string.
- **Do NOT emit any structural keys other than `number`, `type`, `text`, `options`.** The `options` key is emitted only for `type: multiple_choice`; omit it for every other type.

## USER

## The stub

Below is a stub listing every question whose `number` is expected on this page. Fill in `text` and (for `multiple_choice` only) `options` for each entry. **Do NOT add or remove entries** — return exactly the entries below, in the same order, with `number` and `type` copied through unchanged.

```yaml
questions:
$question_stub
```

Return ONLY well-formed YAML matching this shape — no markdown fences in your response, no commentary outside the YAML document. (The fenced YAML blocks in this prompt are for visual highlighting only.)

## Output schema

For each entry in the output:

- `number` — copy verbatim from the stub. String, in quotes.
- `type` — copy verbatim from the stub.
- `text` — complete question text as printed. Use `$...$` for inline math and `$$$$...$$$$` for display math. If the stem is not visible on this page (continued from a previous page, or onto the next), use the empty string `""`.
- `options` — for `type: multiple_choice` only, a list of `{letter, text}` entries (one per printed answer option, in printed order). For every other `type`, **omit the `options` key entirely** — do NOT emit `options: []`.

## LaTeX formatting in `text` and option `text`

Block scalars handle backslashes literally, so write LaTeX commands directly:

- bold text → `\textbf{...}`
- italic text → `\textit{...}`
- unordered lists → `\begin{itemize}\item first\item second\end{itemize}`
- ordered/numbered lists → `\begin{enumerate}\item first\item second\end{enumerate}`
- tables (option grids, binary registers, fill-in cells) → `\begin{tabular}{col-spec} cell & cell \\ next row \end{tabular}` with `\hline` between rows
- inline math → `$...$`; display math → `$$$$...$$$$`
- explicit line breaks between prose sentences → `\newline`
- answer lines that span a full line (printed as a long run of dots in the paper) → `\dotfill`. Inline dots within prose stay as literal text — `\dotfill` is for full-line placeholders only.

Constraints:

- Never use `\newline` immediately after `\begin{...}` or before `\end{...}`.
- Never use more than one `\newline` in a row.
- List items begin directly with `\item` — no `\newline` between items.

## Quoting rules

YAML scalar quoting matters here because question text routinely contains LaTeX backslashes — and a single wrong quote silently destroys them. **Never use double quotes for any string field in the output.** Double quotes interpret `\` as an escape introducer, so `"\texttt{DIV}"` parses to a literal TAB followed by `exttt{DIV}` — silently destroying the LaTeX command.

Apply these rules to BOTH `text:` and each option's `text:`:

- Plain short value with no special characters → no quoting: `text: A particle moves in a circle`, `letter: A`.
- Single-line value containing a backslash (LaTeX commands like `\texttt{DIV}`, `\leftarrow`) → single quotes: `text: '\texttt{DIV}'`. Single quotes do not interpret escapes; the backslash is preserved literally.
- Single-line value containing both a single quote and a backslash, or any multi-line value → block scalar (`|`):

      text: |
        First line with a \texttt{token}
        Second line

WRONG: `text: "\texttt{DIV}"`     ← becomes `<TAB>exttt{DIV}` on parse
RIGHT: `text: '\texttt{DIV}'`     ← preserves `\texttt{DIV}`
RIGHT: block scalar (above)        ← preserves `\texttt{DIV}`

## Worked example

Suppose the stub for this page contains:

```yaml
questions:
  - number: "5"
    type: multiple_choice
    text: ""
  - number: "6"
    type: short_answer
    text: ""
  - number: "7"
    type: calculation
    text: ""
  - number: "8"
    type: short_answer
    text: ""
```

A correct response (one MCQ, one plain short-answer, one calculation with `\dotfill` answer lines, one short-answer with a `\begin{tabular}` option grid):

```yaml
questions:
  - number: "5"
    type: multiple_choice
    text: 'Which quantity has the unit $\text{kg}\,\text{m}\,\text{s}^{-1}$?'
    options:
      - letter: A
        text: energy
      - letter: B
        text: force
      - letter: C
        text: momentum
      - letter: D
        text: power
  - number: "6"
    type: short_answer
    text: State Newton's second law of motion.
  - number: "7"
    type: calculation
    text: |
      Convert the \textbf{two} binary numbers to hexadecimal.

      10010011 \dotfill

      00001101 \dotfill
  - number: "8"
    type: short_answer
    text: |
      Circle \textbf{three} devices that are output devices.

      \begin{tabular}{|l|l|l|}
      \hline
      actuator & digital versatile disk (DVD) & keyboard \\
      \hline
      microphone & mouse & printer \\
      \hline
      scanner & sensor & solid-state drive (SSD) \\
      \hline
      \end{tabular}
```

Notes:
- Entry 5: MCQ shape with the full `{letter, text}` list. The stem is single-quoted because it contains `\text{...}` LaTeX commands.
- Entry 6: plain prose with an apostrophe — YAML plain scalars handle a non-leading apostrophe fine, no quoting needed.
- Entries 6–8: no `options` key at all. Omit it; do not emit `options: []`.
- Entry 7: block scalar (`|`) because the text is multi-line; `\dotfill` per answer line; blank lines inside the block scalar are paragraph breaks.
- Entry 8: block scalar (`|`); `\begin{tabular}` matches the printed grid the candidate circles. `\textbf{three}` (not `**three**`) — emit LaTeX directly per the rules above.

## CODE_FORMATTING

This exam contains code and pseudocode. Question text and answer options must render code in monospace.

In `text:` content (and in `options:` text for multiple-choice questions):
- Wrap inline code tokens (variable names, function calls, single keywords like IF / WHILE / DECLARE / RETURN) in \texttt{...}.
- Wrap multi-line code or pseudocode listings in \begin{alltt}...\end{alltt}; preserve indentation with literal spaces; literal newlines between lines.
- Even a single line like "DECLARE x : INTEGER" or "Counter <- Counter + 1" counts as code and must be wrapped in \texttt{...} (inline) or \begin{alltt}...\end{alltt} (own line).
- NEVER use \textbf{...} for code — bold is not monospace.
- For pseudocode assignment, use the ASCII arrow `<-`. NEVER emit math commands like \leftarrow, \rightarrow, \gets, \to inside alltt — alltt is text mode and these break compilation.

Inside \begin{alltt}...\end{alltt}: do NOT escape <, >, &, %, _, #, $ for LaTeX (alltt is verbatim-with-commands); only escape { → \{, } → \}, backslash → \textbackslash{}.
