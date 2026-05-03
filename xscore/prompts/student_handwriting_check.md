---
name: student_handwriting_check
version: v8
description: Step 14 phase B — student_handwriting_check (matcher). User-only prompt for the per-scan-page vision LLM call. Given the closed vocabularies built from the empty exam (phase A's catalog), the model MATCHES this scan page to one known page type and one known page number — open-ended classification is no longer permitted. Also detects student handwriting (kept to drive step 18 marking_page_register's skip-set). Substitutions $page_type_options and $page_number_options are bullet lists rendered by the caller. Used by xscore.marking.blank_page_detection._has_handwriting. v8 (2026-05) replaced v7's open-ended detection with closed-vocabulary matching against the empty-exam catalog produced by phase A; renamed `is_cover_page` boolean → `page_type` enum and `page_number` (free integer) → matched value from a fixed list with special tokens "cover" / "none". v7 added per-field confidences.
---

This is one page from a student's scanned exam. It may have printed horizontal writing lines, printed headers/footers, printed question text, and student handwriting. It may be the cover page, an answer page, or a fully blank page.

## What you are doing

We have already analyzed the EMPTY exam paper and built two closed vocabularies. Your job is to MATCH this scan page to one entry from each list — you may NOT invent new page types or new page numbers.

## Closed vocabularies

### Page types (pick exactly one)

$page_type_options

Definitions:
- `cover page` — exam title at the top, fields for the student to fill in (Name, Date, Class, Candidate Number, etc.), and NO question text. Cover pages typically have NO printed page number.
- `instruction page` — no name field, no question text. General instructions, formula sheet, candidate notice, periodic table, or similar.
- `question page` — has printed question text.
- `blank page` — contains the words "BLANK PAGE" prominently.

### Page numbers (pick exactly one)

$page_number_options

Plus these two special tokens (use as a string):
- `"cover"` — this page is the cover page (cover pages have no printed page number).
- `"none"` — this is NOT a cover page but no printed page number is visible.

The integers in the list above are the page numbers actually printed in the empty exam, plus a small overflow buffer (3 numbers above the highest detected). If the printed page number you see is NOT in this list, pick `"none"` and explain in `problem`.

## Cross-field constraint

If `page_number` is the string `"cover"`, then `page_type` MUST be `"cover page"` — and vice versa. If you violate this, the post-processor will flag the entry and surface it for manual review.

## Questions

Answer THREE matching questions and THREE confidence questions:

1. PAGE TYPE — which entry from the page-type list above does this scan page best match?
2. PAGE NUMBER — which entry from the page-number list above (including the special tokens `"cover"` / `"none"`) does this scan page best match? Read the typeset page number printed in the margin (top or bottom). Do NOT return question numbers (e.g. "Q3", "Question 5"), section markers, paper codes, or dates — only the printed page number. Cover pages typically have no number; pick `"cover"` for them.
3. STUDENT HANDWRITING — Is there any handwriting (ink written by the student) on this page? Ignore printed lines, printed text, question numbers, page numbers, and the printed material visible in the empty-exam paper. Faint marks that bleed through from the OTHER side of the paper (show-through) do NOT count — only report ink clearly and deliberately written on THIS side. (Cover pages WILL usually have handwriting, since the student fills in their name there.)
4. CONFIDENCE — PAGE TYPE — integer 0..10 (10 = absolutely certain, 5 = cannot decide).
5. CONFIDENCE — PAGE NUMBER — integer 0..10. Lower when the digit is faint, partially cropped, or if you fell back to `"none"`.
6. CONFIDENCE — HANDWRITING — integer 0..10. Lower for marginal/ambiguous marks (faint show-through, partial doodles, smudges).

## Problem field

Fill `problem` with one short sentence (under 25 words) when ANY of:
- any of the three confidences is `< 7`
- a field could not be matched (e.g. the printed page number isn't in the list and you fell back to `"none"`)
- the cross-field constraint is at risk (e.g. you picked `page_number == "cover"` but the page doesn't actually look like a cover)

**Exception**: a page where `page_type == "cover page"` AND `page_number` is `"cover"` (or `"none"`) is the expected, normal case — leave `problem` empty in that situation.

If none of the conditions trigger, leave `problem` as the empty string `""`.

## Return JSON only — exact shape

```
{"page_type": "<one entry from the page-type list>",
 "page_number": <int from the page-number list> | "cover" | "none",
 "has_handwriting": <bool>,
 "confidence_page_type": <int 0..10>,
 "confidence_page_number": <int 0..10>,
 "confidence_handwriting": <int 0..10>,
 "problem": "<one short sentence or empty string>"}
```
