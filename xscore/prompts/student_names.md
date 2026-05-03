---
name: student_names
version: v3
description: Steps 11/15 — student_names. User-only prompt for HANDWRITTEN-name OCR. Two sections — FREEFORM (no roster, no substitutions) and WITH_ROSTER (snap to roster; placeholder $roster is a multi-line "  - <name>" bullet list). v3 merged the former student_names_freeform.md (v1) and student_names_with_roster.md (v2) into one file. Used by xscore.preprocessing.assign_pages_to_students.
---
## FREEFORM

Look at the top of this exam page for the student's HANDWRITTEN name.

Ignore all pre-printed or typed text: exam codes, stamps, watermarks, school names, barcodes, or labels (e.g. printed codes, page numbers, stamps).

Return ONLY a JSON object:
{"name": "Full name as written"}

**Prefer one of these sentinels over guessing a name you are not confident about:**
- `NONAME` — no handwritten name is visible at all (page is blank or only printed text).
- `UNREADABLE` — a handwritten name is visible but you cannot read it confidently.

Examples:
{"name": "NONAME"}
{"name": "UNREADABLE"}

## WITH_ROSTER

Look at the top of this exam page for the student's HANDWRITTEN name.

Ignore all pre-printed or typed text: exam codes, stamps, watermarks, school names, barcodes, or labels (e.g. printed codes, page numbers, stamps).

Here is the official student roster:
$roster

Return ONLY a JSON object with the roster name that best matches the handwritten name, spelled EXACTLY as it appears in the roster above:
{"name": "Full name as written"}

**Prefer one of these sentinels over guessing a roster name you are not confident about:**
- `NONAME` — no handwritten name is visible at all (page is blank or only printed text).
- `UNREADABLE` — a handwritten name is visible but you cannot read it confidently.
- `NOMATCH` — you can read the handwritten name but it is not in the roster. Also use `NOMATCH` when the student wrote their name in a non-Latin script (e.g. Chinese characters) and the roster uses Latin script — script mismatch counts as not-in-roster.

Examples:
{"name": "NONAME"}
{"name": "UNREADABLE"}
{"name": "NOMATCH"}
