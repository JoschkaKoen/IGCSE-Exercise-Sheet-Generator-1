---
name: read_student_list
version: v2
description: Step 03 — read_student_list. SYSTEM holds the schema/rules; USER holds the raw spreadsheet rows (or is empty when the source is a PDF and the file is attached separately). Returns {"names": [...]} JSON. Used by xscore.shared.load_student_list. Returns one entry per row, preserving duplicates by design (the school may have two students with the same first name). v2 documented duplicate-name preservation, added whitespace-trim rule, added worked example.
---
## SYSTEM

Extract every student name from the spreadsheet rows below.

Skip:
- Header rows (e.g. "English name", "Name", "学生").
- Blank rows.
- Footer rows (totals, page numbers).

If the same name appears multiple times, emit each occurrence — duplicates are intentional (the school may have two students with the same first name).

Trim leading and trailing whitespace from each name before emitting.

Return JSON only with this shape: `{"names": [<str>, <str>, ...]}`. One string per student, in the order they appear in the input.

## Worked example

Input rows:
```
English name
Andy
Andy
Bosco

Total: 3
```

Output:
```json
{"names": ["Andy", "Andy", "Bosco"]}
```

Note: header row `English name` skipped, blank row skipped, footer `Total: 3` skipped, two `Andy` entries preserved.

## USER

$csv_data
