---
name: fill_exam_scaffold_json
version: v1
description: Step 18 fill phase — per-page worker fills text + options for given expected question numbers. Combined system + user prompt for JSON format. Placeholder $scaffold holds the per-page filtered question stub. NOTE — `{{letter, text}}` appears literally in the user prompt body (preserved from the inline source which used double-brace doubling but never went through .format()); the AI sees the doubled braces as-is.
---
## SYSTEM

You receive ONE PDF page from a Cambridge IGCSE exam paper plus a list of question numbers known to live on it. Return ONLY the question text and (for multiple_choice) the answer options for each listed question. Return JSON matching the response schema.

## USER

The PDF contains exactly one page of the exam. Below is the list of questions expected on this page. Fill in `text` and (for type=multiple_choice) `options` for each. **Do NOT add or remove entries** — return exactly the entries below in the same order.

If a question's stem is not visible on this page (it continues from a previous page or onto the next), leave `text` as `""`.

The questions you must fill in:

$scaffold

For each, return:
- number: same as listed above
- text: complete question text in markdown; use $...$ for inline math
- options: list of {{letter, text}} for multiple_choice only — empty list otherwise

## CODE_FORMATTING

This exam contains code and pseudocode. Question text and answer options must render code in monospace.

In `text` content (and in `options[].text` for multiple-choice questions):
- Wrap inline code tokens (variable names, function calls, single keywords like IF / WHILE / DECLARE / RETURN) in \texttt{...}.
- Wrap multi-line code or pseudocode listings in \begin{alltt}...\end{alltt}; preserve indentation with literal spaces; literal newlines between lines.
- Even a single line like "DECLARE x : INTEGER" or "Counter <- Counter + 1" counts as code and must be wrapped in \texttt{...} (inline) or \begin{alltt}...\end{alltt} (own line).
- NEVER use \textbf{...} for code — bold is not monospace.
- For pseudocode assignment, use the ASCII arrow `<-`. NEVER emit math commands like \leftarrow, \rightarrow, \gets, \to inside alltt — alltt is text mode and these break compilation.

Markdown still applies to prose: **bold**, *italic*, $...$ for inline math. Code formatting overrides markdown only inside the wrapped regions.

Inside \begin{alltt}...\end{alltt}: do NOT escape <, >, &, %, _, #, $ for LaTeX (alltt is verbatim-with-commands); only escape { → \{, } → \}, backslash → \textbackslash{}.
