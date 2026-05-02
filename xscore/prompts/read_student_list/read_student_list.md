---
name: read_student_list
version: v1
description: Step 03 — read_student_list. SYSTEM holds the schema/rules; USER holds the raw spreadsheet rows (or is empty when the source is a PDF and the file is attached separately). Returns {"names": [...]} JSON. Used by xscore.shared.load_student_list.
---
## SYSTEM

Extract every student name from the spreadsheet rows below.

Skip:
- Header rows (e.g. "English name", "Name", "学生").
- Blank rows.
- Footer rows (totals, page numbers).

Return JSON only with this shape: `{"names": [<str>, <str>, ...]}`. One string per student, in the order they appear in the input.

## USER

$csv_data
