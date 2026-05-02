---
name: student_names_with_roster
version: v1
description: Step 15 — student_names. User-only prompt that asks the vision LLM to extract one student's HANDWRITTEN name and snap it to the official roster. Placeholder $roster is a multi-line bullet list of "  - <name>" entries (Template syntax). Used by xscore.preprocessing.assign_pages_to_students with-roster branch. (Step number was 11 in earlier pipeline versions; current run-folder is `15_student_names`.)
---
Look at the top of this exam page for the student's HANDWRITTEN name.

Ignore all pre-printed or typed text: exam codes, stamps, watermarks, school names, barcodes, or labels (e.g. printed codes, page numbers, stamps).

Here is the official student roster:
$roster

Return ONLY a JSON object with the roster name that best matches the handwritten name, spelled EXACTLY as it appears in the roster above:
{"name": "Full name as written"}

**Prefer one of these sentinels over guessing a roster name you are not confident about:**
- `NONAME` — no handwritten name is visible at all (page is blank or only printed text).
- `UNREADABLE` — a handwritten name is visible but you cannot read it confidently.
- `NOMATCH` — you can read the handwritten name but it is not in the roster.

Examples:
{"name": "NONAME"}
{"name": "UNREADABLE"}
{"name": "NOMATCH"}
