---
name: student_handwriting_check
version: v10
description: Step 15 — student_handwriting_check (matcher). 
---

This is one page from a student's scanned exam. It may have student handwriting on it.

Your job: pick one page type and one page number from the lists below, and say whether the page has any student handwriting. You may NOT invent values — pick from the lists.

## Page types — pick one

$page_type_options

What each one is:
- `cover page` — the title page. Shows the exam title and blank fields for Name, Date, Class, Candidate Number, etc. No question text. Usually no printed page number.
- `instruction page` — general instructions, formula sheet, periodic table, or candidate notice. No name fields, no question text.
- `question page` — has printed question text.
- `blank page` — has the words "BLANK PAGE" printed in the middle.
- `writing space page` — mostly empty, with printed lines (solid or dotted) for the student to write on. No question text. Short labels like "Turn over", "Section B", or "Working space" are OK.

If you pick `cover page`, also pick `"cover"` for the page number — and vice versa. They go together.

## Page numbers — pick one

$page_number_options

Or one of these two special tokens (return them as JSON strings, with quotes):
- `"cover"` — this is the cover page. Cover pages have no printed page number.
- `"none"` — not a cover page, but no printed page number is visible.

The integers above are the page numbers actually printed in the empty exam, plus 3 extra above the highest one. If the printed page number you see is not in the list, pick `"none"` and explain in `problem`.

## How to find the page number

This is the tricky one. Read carefully.

- Look ONLY in the top margin and the bottom margin. The page number lives in one of those two strips, often in a corner or the centre.
- It is small and printed number in the same font as the rest of the page. The printed number is NOT big. The page number is NOT handwritten.
- Student handwriting may sit right next to the printed page number, or partly over it. Look past the handwriting for the small printed digit of the page number.
- These are NOT the page number — never return them:
  - question numbers (the bold digit at the start of a question, like the "9" in "9 A resultant force…")
  - section markers (e.g. "Section B")
  - paper codes (e.g. "0625/22/M/J/23")
  - dates (e.g. "M/J/23")
  - any handwritten digits, anywhere on the page
- If you cannot find a small printed page number after checking both margins, pick `"none"` and say so in `problem`. Do NOT guess. Picking a question number as the page number is the most common mistake — avoid it.

## Handwriting — yes or no

Is there any ink the student wrote on this page?

- Ignore everything that was already printed on the empty exam: lines, text, question numbers, page numbers.
- Faint marks bleeding through from the OTHER side of the paper (show-through) do NOT count — only ink clearly written on THIS side.
- Cover pages usually DO have handwriting (the student fills in their name).

## Confidences — integer 0..10 each

- `confidence_page_type` — 10 = certain, 5 = cannot decide.
- `confidence_page_number`:
  - 8–10: clear small printed digit in the top or bottom margin, OR you carefully checked both margins and the page truly has no number (`"none"`).
  - 5–7: handwriting partly covers the digit but you can still read it.
  - 0–4: unsure, or the only digit you found is a question number — pick `"none"` instead.
- `confidence_handwriting` — lower for marginal cases (faint show-through, smudges).

## Problem field

Write one short sentence in `problem` (under 25 words) when ANY of these is true:
- any confidence is below 7
- you picked `"none"` for the page number
- handwriting partly covered the printed page number
- something else looks wrong (e.g. you picked `"cover"` but the page does not look like a cover)

Otherwise leave `problem` as `""`.

A normal cover page (`page_type == "cover page"` AND `page_number == "cover"` or `"none"`) is the expected case — leave `problem` empty for it.

## Return JSON only — exact shape

```
{"page_type": "<one entry from the page-type list>",
 "page_number": <int from the page-number list> | "cover" | "none",
 "has_handwriting": <true | false>,
 "confidence_page_type": <int 0..10>,
 "confidence_page_number": <int 0..10>,
 "confidence_handwriting": <int 0..10>,
 "problem": "<one short sentence or empty string>"}
```

Return only the JSON object. No markdown, no extra text, no explanation.
