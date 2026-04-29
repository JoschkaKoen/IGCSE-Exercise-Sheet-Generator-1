---
name: parse_mark_scheme_yaml
version: v1
description: Step 20 — parse_mark_scheme. Combined system + user prompt for mark-scheme extraction in YAML format. Placeholder $scaffold (Template syntax) holds the question scaffold inserted into the user prompt. Body also contains literal LaTeX math `$...$` — Template's safe_substitute leaves bare `$<non-identifier>` literal; only $scaffold is substituted. Used by xscore.scaffold.formats.yaml_format.YamlScaffoldFormat.
---
## SYSTEM

You are an expert at reading Cambridge IGCSE mark schemes. Extract marking criteria as structured YAML.

## USER

Return ONLY well-formed YAML, no markdown fences or other text outside the YAML.

Below is a scaffold listing every question from the exam. Fill in `correct_answer` and add `criteria` entries for each question, based on the mark scheme.

$scaffold

For each question:
- `correct_answer`: always a non-empty string — the model/expected answer. For multiple-choice: just the letter (e.g. "C"). For questions with a single definitive answer: that answer (e.g. "930D", "00001111"). For "any N from" / open-ended questions: write a brief sample answer derived from the criteria (e.g. "Actuator, Printer, Speaker" or "Any three from: A, B, C"). Never leave this empty or null. `correct_answer` is the **final answer value**, not the working / derivation / arithmetic tableau. For binary-arithmetic questions, the answer is the resulting binary number (e.g. `correct_answer: '10101011'`) — the addition layout, carries, and intermediate steps belong in `criteria`. Pseudocode-as-answer (where the question asks "write pseudocode that …") is fine as a multi-line block scalar; that is an answer value, not working.
  Quoting rules — **never use double quotes** for `correct_answer` (double quotes interpret `\` as an escape introducer, so `\newline` becomes a real newline + `ewline` and `\leftarrow` errors out):
  - Plain short value with no special characters → no quoting: `correct_answer: C`, `correct_answer: 930D`, `correct_answer: SongNumber`.
  - Single-line value containing `:` but no backslashes / quotes / newlines → single quotes: `correct_answer: '18 (: 1)'`.
  - Value containing backslashes (LaTeX commands like `\newline`, `\leftarrow`, `\textbf`), `"`, `'`, or newlines → use a block scalar `|`, same as `criteria` (block scalars preserve backslashes and braces literally):

        correct_answer: |
          DECLARE P : STRING
          P \leftarrow "The world"
          DECLARE Q : CHAR
          Q \leftarrow 'W'
  - **If a multi-line `correct_answer` is genuinely required and any line's leading-space count differs from another's** (binary-arithmetic columns, ASCII tables, code with significant indentation): wrap the content in `\begin{alltt}...\end{alltt}` and indent **every** YAML content line of the block scalar to the same depth. YAML's block-scalar indent rule terminates the scalar the moment a content line is indented less than the first content line — so column-aligned visual layout cannot live inside a raw block scalar. Move the alignment inside alltt, where it is plain text:

        correct_answer: |
          \begin{alltt}
               1 1 1 
               0 0 1 1 0 0 1 1
             + 0 1 1 1 1 0 0 0
               1 0 1 0 1 0 1 1
          \end{alltt}
- `criteria`: a YAML list of `{mark: "", criterion: "..."}` entries — use a block scalar (`|`) for each criterion to preserve LaTeX backslashes and braces literally.
  Extract the COMPLETE marking scheme text — introductory sentences, bullet lists, numbered lists, tables, bold text, all mark scheme text. Do not skip any text.

LaTeX formatting rules for criterion text (block scalars handle backslashes literally):
    bold text           → \textbf{...}
    unordered lists     → \begin{itemize}\item first\item second\end{itemize}
    ordered/numbered lists → \begin{enumerate}\item first\item second\end{enumerate}
    tables              → \begin{tabular}{col-spec} cell & cell \\\ next row \end{tabular}
    inline math         → $...$
    Use \newline for explicit line breaks between prose sentences only.
    NEVER use \newline immediately after \begin{...} or before \end{...}.
    NEVER use more than one \newline in a row.
    List items begin directly with \item — no \newline between them.
    Correct: \begin{itemize}\item first\item second\end{itemize}
    plain prose and introductory sentences are written verbatim

- For multiple_choice questions: set `correct_answer` only; `criteria: []`
- Keep every question present — even if marks cannot be found for it

## CODE_FORMATTING

This exam contains code and pseudocode. Mark scheme `correct_answer` and `criterion` text must render code in monospace.

In `correct_answer` and `criterion` text:
- Wrap inline code tokens (variables, function calls, code keywords) in \texttt{...}.
- Wrap multi-line code blocks in \begin{alltt}...\end{alltt}; preserve indentation with literal spaces; do NOT use \textbf for code.
- Inside \begin{alltt}...\end{alltt}: do NOT escape <, >, &, %, _, #, $; only escape { → \{, } → \}, backslash → \textbackslash{}.
