---
name: parse_exam_pdf_user_json
version: v1
description: Step 18 — parse_exam_pdf. User prompt for the JSON-format exam extraction. No substitutions. Used by xscore.scaffold.formats.json_format.JsonScaffoldFormat.build_exam_prompt (returned verbatim or after a layout-aware header). NOTE — `{{letter, text}}` appears literally in the prompt body (preserved from the inline source which used double-brace doubling but never went through .format()); the AI sees the doubled braces as-is.
---
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
