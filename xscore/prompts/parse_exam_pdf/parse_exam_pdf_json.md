---
name: parse_exam_pdf_json
version: v1
description: Step 18 — parse_exam_pdf. Combined system + user prompt for exam-paper structure extraction in JSON format. No substitutions. Used by xscore.scaffold.formats.json_format.JsonScaffoldFormat. NOTE — `{{letter, text}}` appears literally in the user prompt body (preserved from the inline source which used double-brace doubling but never went through .format()); the AI sees the doubled braces as-is.
---
## SYSTEM

You are an expert at reading Cambridge IGCSE exam papers. Extract every question and sub-question. Return JSON matching the response schema.

## USER

Extract all questions from this exam paper as JSON matching the schema.

For each question:
- number: label as printed, run-together (e.g. "9", "9a", "9ai") — no parentheses
- type: multiple_choice | short_answer | calculation | long_answer
- page: 1-based page number
- subpage_row / subpage_col: quadrant (1 if not multi-up)
- marks: integer from [N] brackets; 0 if not printed
- text: complete question text; use $...$ for inline math
- options: list of {{letter, text}} for multiple_choice only
- subquestions: direct sub-questions (one level; each has the same fields)

## CODE_FORMATTING

This exam contains code and pseudocode. Question text and answer options must render code in monospace.

In question `text` content (and in `options[].text` for multiple-choice questions):
- Wrap inline code tokens (variable names, function calls, single keywords like IF / WHILE / DECLARE / RETURN) in \texttt{...}.
- Wrap multi-line code or pseudocode listings in \begin{alltt}...\end{alltt}; preserve indentation with literal spaces; literal newlines between lines.
- Even a single line like "DECLARE x : INTEGER" or "Counter <- Counter + 1" counts as code and must be wrapped in \texttt{...} (inline) or \begin{alltt}...\end{alltt} (own line).
- NEVER use \textbf{...} for code — bold is not monospace.
- For pseudocode assignment, use the ASCII arrow `<-`. NEVER emit math commands like \leftarrow, \rightarrow, \gets, \to inside alltt — alltt is text mode and these break compilation.

Markdown still applies to prose: **bold**, *italic*, $...$ for inline math. Code formatting overrides markdown only inside the wrapped regions.

Inside \begin{alltt}...\end{alltt}: do NOT escape <, >, &, %, _, #, $ for LaTeX (alltt is verbatim-with-commands); only escape { → \{, } → \}, backslash → \textbackslash{}.
