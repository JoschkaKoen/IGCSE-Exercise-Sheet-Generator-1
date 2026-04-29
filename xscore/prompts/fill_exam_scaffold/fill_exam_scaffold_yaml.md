---
name: fill_exam_scaffold_yaml
version: v1
description: Step 18 fill phase — per-page worker fills text + options for given expected question numbers. Combined system + user prompt for YAML format. Placeholder $scaffold holds the per-page filtered question stub.
---
## SYSTEM

You receive ONE PDF page from a Cambridge IGCSE exam paper plus a list of question numbers known to live on it. Return ONLY the question text and (for multiple_choice) the answer options for each listed question. Preserve markdown / math formatting.

## USER

The PDF contains exactly one page of the exam. Below is a stub listing every question whose number is expected on this page. Fill in `text` and (for type=multiple_choice) `options` for each listed question. **Do NOT add or remove entries** — return exactly the entries below in the same order.

If a question's stem is not visible on this page (it continues from a previous page or onto the next), leave its `text` as `""`.

Return ONLY well-formed YAML in this shape:

questions:
$scaffold

For each entry, fill in:
- `text`: complete question text in markdown; $...$ for inline math, $$...$$ for display math
- `options`: list of `{letter, text}` for multiple_choice only — leave empty otherwise

Use block scalars (`|`) for `text` fields when the text contains newlines or backslashes.

## CODE_FORMATTING

This exam contains code and pseudocode. Question text and answer options must render code in monospace.

In `text:` content (and in `options:` text for multiple-choice questions):
- Wrap inline code tokens (variable names, function calls, single keywords like IF / WHILE / DECLARE / RETURN) in \texttt{...}.
- Wrap multi-line code or pseudocode listings in \begin{alltt}...\end{alltt}; preserve indentation with literal spaces; literal newlines between lines.
- Even a single line like "DECLARE x : INTEGER" or "Counter <- Counter + 1" counts as code and must be wrapped in \texttt{...} (inline) or \begin{alltt}...\end{alltt} (own line).
- NEVER use \textbf{...} for code — bold is not monospace.
- For pseudocode assignment, use the ASCII arrow `<-`. NEVER emit math commands like \leftarrow, \rightarrow, \gets, \to inside alltt — alltt is text mode and these break compilation.

Markdown still applies to prose: **bold**, *italic*, $...$ for inline math. Code formatting overrides markdown only inside the wrapped regions.

Inside \begin{alltt}...\end{alltt}: do NOT escape <, >, &, %, _, #, $ for LaTeX (alltt is verbatim-with-commands); only escape { → \{, } → \}, backslash → \textbackslash{}.
