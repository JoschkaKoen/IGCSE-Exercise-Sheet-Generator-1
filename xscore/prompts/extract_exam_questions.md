---
name: extract_exam_questions
version: v8
description: Step 20 — per-page worker fills text + options for the question numbers extracted in step 19. Combined system + user prompt. Placeholder $question_stub holds the per-page filtered question stub. SYSTEM has named sub-blocks (In scope / What NOT to change); USER has named sub-blocks (The stub / Output schema / Step-20 specifics / Worked example). v8 forced two shapes for `text` and `options[].text` — `''` (empty) or `|` block scalar (non-empty). Removes v7's single-quoted-vs-block decision tree, eliminating the colon-space failure mode for option text; the same `|` shape applies uniformly across all model-authored free-text fields. The `letter` field in option entries (A/B/C/D) stays as plain — it's a structural enum, not free text. v7 renamed the include placeholder `$include_latex_yaml_style` → `$include_shared_latex_rules` (the fragment moved from `_shared/latex_yaml_style.md` to `shared_latex_rules.md`). v6 replaced the inlined LaTeX/quoting/code-formatting rules with the shared fragment, keeping only the step-20-specific bits (`\dotfill`, `$$$$...$$$$` display-math reminder, CS pseudocode `<-` arrow). v5 replaced the "leave text empty if continued" rule with a STUB ERROR flag, trimmed the worked example from 4 entries to 2 (MCQ + calculation), and trimmed the WRONG/RIGHT pairs from 3 to 2. v4 fixes the display-math instruction. v3 switched the output convention from markdown-for-prose to raw LaTeX.
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

- **`\dotfill` for full-line answer lines.** Cambridge papers print long runs of dots where the candidate writes their answer. Render those as `\dotfill` (one per answer line). Inline dots within prose stay as literal text — `\dotfill` is for full-line placeholders only.
- **Display math** uses `$$$$...$$$$` in this prompt source (the loader's `string.Template` treats `$$` as the escape for a literal `$`, so the rendered prompt shows `$$...$$`). Inline math is `$...$`.
- **CS pseudocode.** This exam contains code and pseudocode. Common keywords seen as inline code: `IF` / `WHILE` / `DECLARE` / `RETURN` / `FOR` / `NEXT` / `ENDIF` / `ENDWHILE`. Wrap each in `\texttt{...}`. For pseudocode assignment use the ASCII arrow `<-`; NEVER emit math commands like `\leftarrow`, `\rightarrow`, `\gets`, `\to` inside alltt — alltt is text mode and these break compilation.

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
```

A correct response (one MCQ, one calculation with `\dotfill` answer lines):

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

      10010011 \dotfill

      00001101 \dotfill
```

Notes:
- Every non-empty `text` and option `text` uses `|` block scalar — the same shape regardless of length, single-line or multi-line, presence of LaTeX commands, or any other content judgement. `letter` stays plain (it's a structural enum, not free text).
- Entry 7 omits the `options` key entirely (non-MCQ); do NOT emit `options: []`. Blank lines inside the block scalar are paragraph breaks; `\dotfill` per answer line.
