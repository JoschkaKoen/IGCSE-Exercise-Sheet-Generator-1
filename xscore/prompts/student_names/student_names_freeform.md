---
name: student_names_freeform
version: v1
description: Step 11 — student_names. User-only prompt that asks the vision LLM to extract one student's HANDWRITTEN name from the top of an exam page. Freeform variant — used when no roster is available. No substitutions. Used by xscore.preprocessing.assign_pages_to_students freeform branch.
---
Look at the top of this exam page for the student's HANDWRITTEN name.

Ignore all pre-printed or typed text: exam codes, stamps, watermarks, school names, barcodes, or labels (e.g. printed codes, page numbers, stamps).

Return ONLY a JSON object:
{"name": "Full name as written"}

If no handwritten name is visible or the name field is blank, return:
{"name": ""}
