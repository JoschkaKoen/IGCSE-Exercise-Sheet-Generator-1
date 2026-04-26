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
