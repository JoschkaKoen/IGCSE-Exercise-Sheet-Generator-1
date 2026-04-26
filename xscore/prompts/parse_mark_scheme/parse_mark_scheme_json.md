---
name: parse_mark_scheme_json
version: v1
description: Step 20 — parse_mark_scheme. Combined system + user prompt for mark-scheme extraction in JSON format. Placeholder $scaffold (Template syntax) holds the question scaffold inserted into the user prompt. JSON-string backslashes appear as \\ (two literal backslashes — needed because the AI emits them inside JSON strings). Body has `$v` in the LaTeX example; Template's safe_substitute leaves it literal as long as no `v=` is passed (only $scaffold is intended). Used by xscore.scaffold.formats.json_format.JsonScaffoldFormat.
---
## SYSTEM

You are an expert at reading Cambridge IGCSE mark schemes. Extract marking criteria. Return JSON matching the response schema.

## USER

For each question in the scaffold below, fill in `correct_answer` and `criteria` based on the mark scheme.

$scaffold

- `correct_answer`: always a non-empty string — the model/expected answer. For multiple-choice: just the letter (e.g. "C"). For questions with a single definitive answer: that answer (e.g. "930D", "00001111"). For "any N from" / open-ended questions: write a brief sample answer derived from the criteria (e.g. "Actuator, Printer, Speaker" or "Any three from: A, B, C"). Never leave this empty or null.
- `criteria`: list of {mark, criterion} — extract the COMPLETE marking scheme text.

LaTeX in criterion strings: use \\ for backslash in JSON strings.
Examples: "\\textbf{word}", "$v = 2\\pi r / T$"
Inline code: "\\texttt{...}". Multi-line code: "\\begin{alltt}...\\end{alltt}" (do not use \\textbf for code).
