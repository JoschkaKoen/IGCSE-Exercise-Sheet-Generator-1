---
name: parse_mark_scheme_yaml
version: v3
description: Step 23 — parse_mark_scheme. Combined system + user prompt for mark-scheme extraction in YAML format. Placeholder $scaffold (Template syntax) holds the full exam scaffold inserted into the user prompt; per-page workers fill criteria for questions on the current page and leave the rest empty. Body also contains literal LaTeX math `$...$` — Template's safe_substitute leaves bare `$<non-identifier>` literal; only $scaffold is substituted. v3 changed the MCQ rule from `criteria: []` to a single `mark: 0` entry containing the mark scheme's explanation of the correct answer, formatted as a LaTeX itemize list — downstream code (`scaffold_xml._merge_scheme`) routes that text into the new `Question.reasoning` field for MCQ. v2 restructured SYSTEM into named sub-blocks (In scope / What NOT to change) and USER into named sub-blocks (The scaffold / Output schema / correct_answer rules / Quoting rules / criteria rules / LaTeX formatting in criterion text / Worked example). Used by xscore.scaffold.formats.yaml_format.YamlScaffoldFormat.
---
## SYSTEM

You are an expert at reading exam mark schemes. Your job is to extract per-question marking criteria from the mark scheme page you receive — what the model answer is, and what gains marks.

## In scope

- For each question listed in the scaffold (in the user message): fill in `correct_answer` (the model/expected answer) and `criteria` (a list of `{mark, criterion}` entries — what gains marks).
- Extract the COMPLETE marking scheme text — introductory sentences, bullet lists, numbered lists, tables, bold text, all mark scheme text. Do not skip any text.
- The page may arrive as a rendered PDF, an extracted-text rendering of the PDF, or a rasterised image — treat all three as "this page of the mark scheme".

## What NOT to change

- **Keep every scaffold entry.** The scaffold lists every question in the exam; return one entry per scaffold entry, in the same order, with `number`, `type`, and `marks` copied through unchanged. Do not drop any.
- **Per-page filtering.** The scaffold is the FULL exam scaffold, but the page in front of you only contains criteria for some of the questions. For questions whose criteria are NOT on this page, leave `correct_answer: ""` and `criteria: []` (the scaffold's empty defaults) — do not guess.
- **Do NOT emit any structural keys other than `number`, `type`, `marks`, `correct_answer`, `criteria`.**

## USER

## The scaffold

Below is the full exam scaffold. Fill `correct_answer` and `criteria` for the questions whose criteria appear on this page; for every other question, leave `correct_answer: ""` and `criteria: []` (the scaffold's empty defaults). **Keep every entry** — copy `number`, `type`, and `marks` through unchanged.

```yaml
$scaffold
```

Return ONLY well-formed YAML matching this shape — no markdown fences in your response, no commentary outside the YAML document. (The fenced YAML blocks in this prompt are for visual highlighting only.)

## Output schema

For each entry in the output:

- `number`, `type`, `marks` — copy verbatim from the scaffold.
- `correct_answer` — the final answer value the candidate is expected to give. See `## correct_answer rules` below. For questions whose criteria are not on this page, use the empty string `""` (the scaffold's empty default; round-trip unchanged).
- `criteria` — a YAML list of `{mark, criterion}` entries, one per item in the printed mark scheme. See `## criteria rules` below. For `multiple_choice` questions, put the mark scheme's explanation of the correct answer (typically a short bulleted breakdown) into a single `criteria` entry with `mark: 0` (the mark belongs to the `correct_answer` letter, not the explanation). Format the explanation as a LaTeX itemize list inside a block scalar — see the worked example for Question 1. If the page does not include an explanation, use `criteria: []`. For questions whose criteria are not on this page, also `criteria: []`.

## correct_answer rules

`correct_answer` is the **final answer value** — not the working, derivation, or arithmetic tableau (those go in `criteria`).

- Multiple-choice: just the letter, e.g. `correct_answer: C`.
- Single definitive answer: that answer, e.g. `correct_answer: 930D`, `correct_answer: '00001111'`.
- "Any N from" / open-ended: a brief sample answer, e.g. `correct_answer: 'Actuator, Printer, Speaker'` or `correct_answer: 'Any three from: A, B, C'`.
- Binary arithmetic: the resulting binary number, e.g. `correct_answer: '10101011'`. The addition layout, carries, and intermediate steps belong in `criteria`.
- Pseudocode-as-answer (where the question asks "write pseudocode that …"): the pseudocode itself is the answer value, in a multi-line block scalar.

For questions whose criteria are not on this page, set `correct_answer: ""` (matching the scaffold's empty default, per `## What NOT to change`).

## Quoting rules

**Never use double quotes for any non-empty string field.** Double quotes interpret `\` as an escape introducer in YAML, so `"\newline"` becomes a real newline + `ewline` and `"\leftarrow"` errors out — silently destroying every LaTeX command. (Empty `""` is fine — there's no `\` to misinterpret.)

The bullets below cover when to use plain / single-quoted / block-scalar form for `correct_answer`. Each `criterion` is always a block scalar (per `## criteria rules`), so its quoting form is fixed — the never-double-quote rule applies trivially there.

- Plain short value with no special characters → no quoting: `correct_answer: C`, `correct_answer: 930D`, `correct_answer: SongNumber`.
- Single-line value containing `:` (colon-space), `'`, `\textbf`, or any backslash → single quotes: `correct_answer: '18 (: 1)'`, `correct_answer: '\leftarrow'`. Single quotes do not interpret escapes; the backslash is preserved literally.
- Single-line value containing both a single quote and a backslash, or any multi-line value → block scalar (`|`), which preserves backslashes and braces literally:

      correct_answer: |
        DECLARE P : STRING
        P \leftarrow "The world"
        DECLARE Q : CHAR
        Q \leftarrow 'W'

WRONG: `correct_answer: "\leftarrow"`     ← becomes a TAB-prefixed `eftarrow` on parse
RIGHT: `correct_answer: '\leftarrow'`     ← preserves `\leftarrow`
RIGHT: block scalar (above)               ← preserves `\leftarrow`

### Column-aligned content (binary arithmetic, ASCII tables, indented code)

YAML's block-scalar indent rule terminates the scalar the moment a content line is indented less than the first content line — so column-aligned visual layout cannot live inside a raw block scalar. Wrap such content in `\begin{alltt}...\end{alltt}` and indent **every** YAML content line of the block scalar to the same depth. The alignment lives inside `alltt`, where it is plain text:

      correct_answer: |
        \begin{alltt}
             1 1 1 
             0 0 1 1 0 0 1 1
           + 0 1 1 1 1 0 0 0
             1 0 1 0 1 0 1 1
        \end{alltt}

## criteria rules

`criteria` is a YAML list of `{mark, criterion}` entries — one entry per item in the printed mark scheme. Use a block scalar (`|`) for each `criterion` to preserve LaTeX backslashes and braces literally.

Extract the COMPLETE marking scheme text for each question — introductory sentences, bullet lists, numbered lists, tables, bold text. Do not skip any text. Do not paraphrase or summarise.

## LaTeX formatting in `criterion` text

Block scalars handle backslashes literally, so write LaTeX commands directly:

- bold text → `\textbf{...}`
- unordered lists → `\begin{itemize}\item first\item second\end{itemize}`
- ordered/numbered lists → `\begin{enumerate}\item first\item second\end{enumerate}`
- tables → `\begin{tabular}{col-spec} cell & cell \\ next row \end{tabular}`
- inline math → `$...$`
- explicit line breaks between prose sentences → `\newline`

Constraints:

- Never use `\newline` immediately after `\begin{...}` or before `\end{...}`.
- Never use more than one `\newline` in a row.
- List items begin directly with `\item` — no `\newline` between items.
- Plain prose and introductory sentences are written verbatim (no wrapping command needed).

## Worked example

Suppose the scaffold for the exam contains three questions:

```yaml
questions:
  - number: "1"
    type: multiple_choice
    marks: 1
    correct_answer: ""
    criteria: []
  - number: "2"
    type: short_answer
    marks: 0
    correct_answer: ""
    criteria: []
  - number: "2a"
    type: calculation
    marks: 3
    correct_answer: ""
    criteria: []
```

You receive the page of the mark scheme containing criteria for questions 1 and 2a (but not 2). A correct response:

```yaml
questions:
  - number: "1"
    type: multiple_choice
    marks: 1
    correct_answer: C
    criteria:
      - mark: 0
        criterion: |
          \begin{itemize}
          \item Option C is correct because the upward thrust exceeds the rocket's weight, giving a net upward force.
          \item Options A and B describe a rocket in free fall, with no thrust.
          \item Option D would only be true if thrust and weight were equal.
          \end{itemize}
  - number: "2"
    type: short_answer
    marks: 0
    correct_answer: ""
    criteria: []
  - number: "2a"
    type: calculation
    marks: 3
    correct_answer: '12.5 m/s'
    criteria:
      - mark: 1
        criterion: |
          \textbf{One mark for} substituting values into the formula:
          $v = \frac{d}{t} = \frac{50}{4}$
      - mark: 1
        criterion: |
          \textbf{One mark for} the correct numerical answer: $12.5$
      - mark: 1
        criterion: |
          \textbf{One mark for} the correct units: m/s
```

Notes:
- Question 1: MCQ — `correct_answer` is just the letter (no quoting); `criteria` contains a single `mark: 0` entry with the mark scheme's explanation as a LaTeX itemize list (so students see why C is correct). Use `criteria: []` only when the mark scheme has no explanation.
- Question 2: parent stem with criteria not on this page — `correct_answer: ""` (the scaffold's empty default, round-tripped), `criteria: []`. Same shape applies to ANY question whose criteria are not on the current page.
- Question 2a: filled — `correct_answer` is single-quoted because it contains a space and a slash. Three `criteria` entries with mark + criterion (LaTeX inline math + `\textbf{...}` inside block scalars).

## CODE_FORMATTING

This exam contains code and pseudocode. Mark scheme `correct_answer` and `criterion` text must render code in monospace.

In `correct_answer` and `criterion` text:
- Wrap inline code tokens (variables, function calls, code keywords) in \texttt{...}.
- Wrap multi-line code blocks in \begin{alltt}...\end{alltt}; preserve indentation with literal spaces; do NOT use \textbf for code.
- Inside \begin{alltt}...\end{alltt}: do NOT escape <, >, &, %, _, #, $; only escape { → \{, } → \}, backslash → \textbackslash{}.
