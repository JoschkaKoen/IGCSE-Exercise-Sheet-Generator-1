---
name: parse_mark_scheme_user_json
version: v1
description: Step 20 — parse_mark_scheme. User prompt for the JSON-format mark-scheme extraction. {scaffold} is a Python str.format placeholder (NOT Template syntax); literal LaTeX braces are escaped as {{ and }}, and JSON-string backslashes appear as \\ (two literal backslashes in the .md). Callers do `body.format(scaffold=...)` after loading. Used by xscore.scaffold.formats.json_format.JsonScaffoldFormat.build_scheme_user_msg.
---
For each question in the scaffold below, fill in `correct_answer` and `criteria` based on the mark scheme.

{scaffold}

- `correct_answer`: always a non-empty string — the model/expected answer. For multiple-choice: just the letter (e.g. "C"). For questions with a single definitive answer: that answer (e.g. "930D", "00001111"). For "any N from" / open-ended questions: write a brief sample answer derived from the criteria (e.g. "Actuator, Printer, Speaker" or "Any three from: A, B, C"). Never leave this empty or null.
- `criteria`: list of {{mark, criterion}} — extract the COMPLETE marking scheme text.

LaTeX in criterion strings: use \\ for backslash in JSON strings.
Examples: "\\textbf{{word}}", "$v = 2\\pi r / T$"
