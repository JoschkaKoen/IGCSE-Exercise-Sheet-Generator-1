---
name: extract_exam_questions
version: v13
description: Step 20 — per-page worker fills text + options for the question numbers extracted in step 19. Combined system + user prompt. Placeholder $question_stub holds the per-page filtered question stub. SYSTEM has named sub-blocks (In scope / What NOT to change); USER has named sub-blocks (The stub / Output schema / Step-20 specifics / Answer-area rendering / Worked example). v13 split the v12 "preserve content tables" rule into two cases: word/option lists (where the grid is just compact printing — words student picks from in "circle three" / "fill the blanks from this list" / "tick one box" style questions) MUST be emitted as `\begin{itemize}\item ...\item ...\end{itemize}` with bullets and blank lines before/after, while data tables (where cell POSITION carries meaning — e.g. an 8-cell binary register pre-populated with values, a truth table, a coordinate grid) MUST stay as `\begin{tabular}`. Worked-example Q9 (cookies word list) updated from `\begin{tabular}{ccc}` to `\begin{itemize}` accordingly. v12 made three refinements after v11 output: (a) strengthened the content-table rule to "ALWAYS emit `\begin{tabular}` when the source shows a table — NEVER fall back to a vertical list of items" (in v11 the model dropped Q1's word list to a vertical list when it couldn't reconcile the column count, instead of fixing the table); (b) made the blank-line-after-instruction rule explicit: ALWAYS leave one blank line between the instruction prompt and the first answer-area marker (whether numbered slots or value-blank pairs), and NEVER leave a blank line between consecutive markers — `Give three reasons:\n\n1. ___\n2. ___\n3. ___` not `Give three reasons:\n1. ___\n2. ___\n3. ___` and not `Give three reasons:\n\n1. ___\n\n2. ___`; (c) switched numbered-slot format from `(1)`, `(2)`, `(3)` to `1.`, `2.`, `3.` (matches Cambridge paper convention; the period after the digit is mandatory). v11 fixed three issues seen in v10 output: (a) added a "content-table cell-count consistency" rule under `## Answer-area rendering` so the model doesn't emit a row with more cells than the column spec declares (caused xelatex `Extra alignment tab` errors in every per-student landscape PDF for s23_12 because Q1's word list was emitted with a 4-cell row in a 3-col table); (b) removed the blank lines between consecutive value-blank pairs in the worked-example Q7 — the model was faithfully copying the spacing and producing a paragraph gap between `10010011 \hrulefill` and `00001101 \hrulefill`; (c) reinforced in the notes that numbered slots must keep the `(1)`, `(2)`, `(3)` parens (the model occasionally dropped them in v10, e.g. emitting `1 \hrulefill` instead). v10 dropped v9's `\textit{(working space)}` marker (no marker at all for working-space headers); added a rule to strip subquestion label prefixes at the start of text (`(i)`, `(ii)`, `(iii)`, `(a)`, `(b)`, `1.`, `2.` — the question number conveys this already); switched numbered-slot markers from inline `\quad`-separated short underlines to one slot per line with `\hrulefill` (each slot's rule fills the cell width); switched value-followed-by-blank from `value~\underline{\hspace{1.5em}}` to `value \hrulefill` for the same cell-width-fill reason; **dropped any standalone marker** below open-ended answer areas — the question prompt + marks count is enough. The short `\underline{\hspace{1.5em}}` is now used only for inline blanks within prose (e.g., the cookies sentence). v9 replaced v8's `\dotfill` rule with a compact answer-area rendering scheme so question text fits narrow report columns (4.5 cm) and the standalone exam_questions.pdf without `\dotfill` expanding to fill the line. New `## Answer-area rendering` section: one uniform marker `\underline{\hspace{1.5em}}` for every blank (single line / multi-line / inline / after-a-value); `\textit{(working space)}` for "Working space" headers (subsumes any dotted lines that follow); `(1)~marker\quad(2)~marker...` inline for numbered answer slots; `\framebox{$\square\,\square...\,\square$}` for empty answer tables (blank registers); content tables (cells with words/numbers/labels) kept verbatim. Worked example expanded from 2 entries (MCQ + calculation) to 5 (adds empty register, content table coexisting with inline blanks, numbered slots) so each pattern is concretely demonstrated. Information preserved — only the LaTeX shape changes; YAML schema unchanged. v8 forced two shapes for `text` and `options[].text` — `''` (empty) or `|` block scalar (non-empty). Removes v7's single-quoted-vs-block decision tree, eliminating the colon-space failure mode for option text; the same `|` shape applies uniformly across all model-authored free-text fields. The `letter` field in option entries (A/B/C/D) stays as plain — it's a structural enum, not free text. v7 renamed the include placeholder `$include_latex_yaml_style` → `$include_shared_latex_rules` (the fragment moved from `_shared/latex_yaml_style.md` to `shared_latex_rules.md`). v6 replaced the inlined LaTeX/quoting/code-formatting rules with the shared fragment, keeping only the step-20-specific bits (`\dotfill`, `$$$$...$$$$` display-math reminder, CS pseudocode `<-` arrow). v5 replaced the "leave text empty if continued" rule with a STUB ERROR flag, trimmed the worked example from 4 entries to 2 (MCQ + calculation), and trimmed the WRONG/RIGHT pairs from 3 to 2. v4 fixes the display-math instruction. v3 switched the output convention from markdown-for-prose to raw LaTeX.
---
## SYSTEM

You receive ONE page from an exam paper (Cambridge IGCSE and similar) plus a list of question numbers known to live on it. Your job is to populate `text` and (for `multiple_choice` only) `options` for each listed question — nothing more.

## In scope

- Return the question text and answer options exactly as printed on the page.
- The page may arrive as a rendered PDF, an extracted-text rendering of the PDF, or a rasterised image — treat all three as "this page of the exam".

## What NOT to change

- **Do NOT add or remove questions.** The user message contains a stub listing every question expected on this page; return one entry per stub entry, in the same order, with the same `number` and `type` values.
- **The stub is curated for this page.** Every entry's stem should be visible. If you cannot find a stub entry's stem on this page, this indicates a stub-generation bug upstream — emit a `|` block scalar containing `STUB ERROR` so QA surfaces it. Do NOT guess.
- **Do NOT emit any structural keys other than `number`, `type`, `text`, `options`.** The `options` key is emitted only for `type: multiple_choice`; omit it for every other type.

## USER

## The stub

Below is the stub. Fill in `text` and (for `multiple_choice` only) `options` for each entry.

```yaml
questions:
$question_stub
```

Return ONLY well-formed YAML matching this shape — no markdown fences in your response, no commentary outside the YAML document. (The fenced YAML blocks in this prompt are for visual highlighting only.)

## Output schema

For each entry in the output:

- `number` — copy verbatim from the stub. String, in quotes.
- `type` — copy verbatim from the stub.
- `text` — complete question text as printed. Use `|` block scalar for the value (single-line or multi-line). Use `$...$` for inline math and `$$$$...$$$$` for display math. (The stub is curated to include only questions whose stem appears on this page; if you cannot find a stem, see the STUB ERROR rule in `## What NOT to change`.)
- `options` — for `type: multiple_choice` only, a list of `{letter, text}` entries (one per printed answer option, in printed order). Each option's `letter` stays plain (`letter: A`); each option's `text` value is a `|` block scalar. For every other `type`, **omit the `options` key entirely** — do NOT emit `options: []`.

$include_shared_latex_rules

## Step-20 specifics

- **Display math** uses `$$$$...$$$$` in this prompt source (the loader's `string.Template` treats `$$` as the escape for a literal `$`, so the rendered prompt shows `$$...$$`). Inline math is `$...$`.
- **CS pseudocode.** This exam contains code and pseudocode. Common keywords seen as inline code: `IF` / `WHILE` / `DECLARE` / `RETURN` / `FOR` / `NEXT` / `ENDIF` / `ENDWHILE`. Wrap each in `\texttt{...}`. For pseudocode assignment use the ASCII arrow `<-`; NEVER emit math commands like `\leftarrow`, `\rightarrow`, `\gets`, `\to` inside alltt — alltt is text mode and these break compilation.

## Answer-area rendering

Cambridge papers print scaffolding for the candidate to write into — dotted lines, blank tables, "Working space" headers, numbered answer slots, inline fill-in-the-blank dots, and parenthesised sub-question labels. Transcribe a marker ONLY when it has a meaningful element to its immediate left (a value, a slot number, a word in prose). Drop markers that would stand alone below an open-ended prompt — the question's prompt and its marks count already convey "expect a written answer".

**Two marker shapes** (use these literally):
- `\hrulefill` — a horizontal rule that fills to the right edge of the report cell. Use at end-of-line (after a value, after a numbered-slot label).
- `\underline{\hspace{1.5em}}` — a short fixed-width underline. Use inline within prose where the sentence continues after the gap.

**Do NOT emit:**
- `\dotfill` anywhere.
- `\hspace{N cm}` cells inside tables as a stand-in for blank cells.
- `\textit{(working space)}` or any other transcription of "Working space" headers — drop the header AND the dotted lines below it.
- Subquestion label prefixes at the very start of the question text: `(i)`, `(ii)`, `(iii)`, `(iv)`, `(v)`, `(a)`, `(b)`, `(c)`, `(d)`, `1.`, `2.` — the question's own number already shows its position. Strip them; the prompt should start with the actual instruction (e.g., "State what effect…").
- A standalone marker on its own line below an open-ended prompt (the multi-line writing space under "Describe X" / "State Y" type questions). The prompt + marks count is enough.
- More than one `\newline` in a row (already covered in the shared rules).

**Conventions** (use these LaTeX shapes literally):

| Page shows | Emit |
|---|---|
| Standalone dotted answer line(s) below an open-ended prompt | Nothing — the prompt + marks count carries the meaning |
| "Working space" header (with or without dotted lines) | Nothing — drop entirely |
| Subquestion prefix at start of text (e.g., `(i)`, `(a)`) | Strip — start the text with the actual prompt |
| Inline fill-in-the-blank within prose (e.g. `Cookies are small ........... files`) | `\underline{\hspace{1.5em}}` between the words |
| A value followed by an answer line (e.g. `10010011 ........`) | `10010011 \hrulefill` — value, space, fill rule. When multiple value-blank lines follow an instruction, ALWAYS leave one blank line between the instruction and the first value, and NEVER leave a blank line between consecutive values: `Convert the two binary numbers:\n\n10010011 \hrulefill\n00001101 \hrulefill` |
| Numbered answer slots (e.g. `1 ____\newline 2 ____\newline 3 ____`) | One per line with `\hrulefill`, each on its own YAML line so the renderer puts each on its own visual line and the rule fills the cell width after `N. `: `1. \hrulefill` newline `2. \hrulefill` newline `3. \hrulefill`. ALWAYS leave one blank line between the instruction prompt and the first numbered slot — `Give three reasons:\n\n1. \hrulefill\n2. \hrulefill\n3. \hrulefill`. NEVER leave a blank line between consecutive slots. The period after the digit is mandatory (`1.` not `1`). |
| Empty answer table — cells contain only blank space for the candidate (e.g. an empty 8-bit register) | `\framebox{$\square\,\square\,\square\,\square\,\square\,\square\,\square\,\square$}` — one `\square` symbol per cell, separated by thin space `\,`, all inside one `\framebox`. Single-row only; if the page shows a multi-row blank table it's almost certainly two adjacent registers — emit two `\framebox{...}` side by side |
| Content table — cells contain actual words, numbers, or labels (a list of options to choose from, a register pre-populated with a value, etc.) | Keep the `\begin{tabular}...\end{tabular}` verbatim — these are part of the question content, not answer scaffolding |

**Mental check for table classification:** read each cell. If you'd READ it as part of the question (it has words/numbers/labels) → content table → keep verbatim. If you'd SKIP it because it's blank space for the candidate → empty answer table → emit `\framebox{$\square\,\square...\,\square$}`.

**Word/option lists vs data tables — choose the right shape, NEVER bare lines.**

- **Word/option lists** — a list of choices, terms, or vocabulary the student picks from (e.g. "circle three of the following devices", "fill the blanks using these terms", "tick one box"). The grid layout on the page is just for compact printing; the words have no positional meaning. **Emit as `\begin{itemize}\item ...\item ...\end{itemize}` with bullets, one `\item` per word/term.** ALWAYS leave one blank line BEFORE `\begin{itemize}` and one blank line AFTER `\end{itemize}` so the list is visually separated from surrounding prose.
- **Data tables** — cells whose POSITION carries meaning (e.g. an 8-cell binary register pre-populated with `0 1 1 1 1 0 1 0`, a truth table, a coordinate grid). Emit as `\begin{tabular}{...}` to preserve the grid. Same column-count consistency rule applies.
- **NEVER emit bare consecutive lines** for list items — that loses the grouping and reads as continuous prose.

**Content-table cell-count consistency.** When you emit `\begin{tabular}{...}`, the column-spec letter count (e.g. `ccc` = 3 columns) MUST equal the `&`-separated cell count in EVERY row. Cambridge papers occasionally print what looks like an extra word in a column (visual spacing makes "printer  scanner" look like two cells in one row when other rows have one). KEEP them in ONE cell separated by `\quad` or just a space (`printer \quad scanner`). Do NOT add an `&`. Mismatched cell counts crash xelatex with `! Extra alignment tab has been changed to \cr.` Count cells in every row before finalising the YAML.

## Worked example

Suppose the stub for this page contains:

```yaml
questions:
  - number: "5"
    type: multiple_choice
    text: ""
  - number: "7"
    type: calculation
    text: ""
  - number: "8a"
    type: short_answer
    text: ""
  - number: "9"
    type: short_answer
    text: ""
  - number: "10"
    type: short_answer
    text: ""
```

A correct response:

```yaml
questions:
  - number: "5"
    type: multiple_choice
    text: |
      Which quantity has the unit $\text{kg}\,\text{m}\,\text{s}^{-1}$?
    options:
      - letter: A
        text: |
          energy
      - letter: B
        text: |
          force
      - letter: C
        text: |
          momentum
      - letter: D
        text: |
          power
  - number: "7"
    type: calculation
    text: |
      Convert the \textbf{two} binary numbers to hexadecimal.

      10010011 \hrulefill
      00001101 \hrulefill
  - number: "8a"
    type: short_answer
    text: |
      Complete the binary register to show its contents after this logical right shift.

      \framebox{$\square\,\square\,\square\,\square\,\square\,\square\,\square\,\square$}
  - number: "9"
    type: short_answer
    text: |
      Complete the statements about cookies. Use the terms from the list.

      \begin{itemize}
      \item compression
      \item executable
      \item HTML
      \item persistent
      \item session
      \item web browser
      \end{itemize}

      Cookies are small \underline{\hspace{1.5em}} files that are sent between a \underline{\hspace{1.5em}} and a \underline{\hspace{1.5em}}.
  - number: "10"
    type: short_answer
    text: |
      Give \textbf{three} reasons why a programmer may use hexadecimal to represent binary numbers.

      1. \hrulefill
      2. \hrulefill
      3. \hrulefill
```

Notes:
- Every non-empty `text` and option `text` uses `|` block scalar — same shape regardless of length, single-line or multi-line, presence of LaTeX commands. `letter` stays plain (structural enum). Non-MCQ entries omit the `options` key entirely; do NOT emit `options: []`.
- Entry 7 — `value \hrulefill` for each binary input (the rule fills to the cell's right edge). The "Working space" booklet header on the printed page is NOT transcribed — drop entirely. No standalone marker below the conversion area. **One blank line between the instruction and the first value**, and **single newline (no blank line) between consecutive value-blank lines** — `instruction\n\n10010011 \hrulefill\n00001101 \hrulefill`. The first blank line is a paragraph break giving the values visual separation from the prompt; the consecutive values stay tight together.
- Entry 8a — empty 8-bit register: a single `\framebox{$\square\,\square...\,\square$}` with thin spaces between squares for visual separation. Do NOT emit a `\begin{tabular}` with `\hspace` cells.
- Entry 9 — word list emitted as `\begin{itemize}` with bullets (one `\item` per term) because the source's grid layout is just compact printing; the words have no positional meaning. **Blank line before AND after the itemize block** so it's visually separated from the surrounding prose. Inline `\underline{\hspace{1.5em}}` markers preserve sentence structure for the fill-in-the-blank statements that follow — short fixed width is correct here because the prose continues to the right of each gap.
- Entry 10 — numbered answer slots STACKED, one per line, each ending in `\hrulefill`. **One blank line between the instruction and the first slot**, then single newlines between consecutive slots — `instruction\n\n1. \hrulefill\n2. \hrulefill\n3. \hrulefill`. Do NOT use `\quad` to put slots inline. **The slot label is `1.`, `2.`, `3.` — a bare digit followed by a period (matches Cambridge paper convention). The period is mandatory; do NOT use parens like `(1)` and do NOT drop the period to write bare `1 \hrulefill`.**

If the source page for a single open-ended prompt (e.g., `Describe how X works.`) shows multi-line writing space below, do NOT emit any marker — the prompt + marks count carries the meaning.

If the source page shows `(i) State what effect…` for a subquestion labelled `i`, transcribe just `State what effect…` (strip the `(i) ` prefix).
