---
name: student_handwriting_check
version: v6
description: Step 14 — student_handwriting_check. User-only prompt for the vision LLM call that checks one scanned exam page for student handwriting, reads the printed page number, AND identifies whether the page is the exam cover page. No substitutions. Used by xscore.marking.blank_page_detection._has_handwriting. v6 promoted the cross-field constraint to a top-level section and added two `reason` examples (good vs bad). v5 renamed `answer` to `has_handwriting`, hard-bound `page_number: null` on cover pages, normalised the confidence scale to [0, 10] to match step 29, and dropped the conditional on the `reason` field.
---

This is one page from a student's scanned exam. It may have printed horizontal writing lines, printed headers/footers, printed question text, and student handwriting. It may be the exam cover page, an answer page, or a fully blank page.

## Cross-field constraints

If `is_cover_page` is true, `page_number` MUST be null. Cover pages don't print page numbers — by convention the cover is page 1, and any digits visible on the cover (e.g. "This document has 12 pages") are NOT this page's number.

## Questions

Answer FIVE questions about THIS page:

1. STUDENT HANDWRITING — Is there any handwriting (ink written by the student) on this page? Ignore printed lines, printed text, question numbers, and page numbers. Faint marks that bleed through from the OTHER side of the paper (show-through) do NOT count — only report ink clearly and deliberately written on THIS side.

2. PRINTED PAGE NUMBER — What is the typeset page number printed on this page (usually in the very top margin or very bottom margin, often centered or in a corner)? Return only the integer. Do NOT return question numbers (e.g. "Q3", "Question 5"), section markers, or dates — only the page number. If formatted as "Page 5 of 30", return 5. If no printed page number is visible, or you cannot read it confidently, return null. (Reminder: see `## Cross-field constraints` above when `is_cover_page` is true.)

3. COVER PAGE — Is this the exam cover page? Cover pages typically have: the exam title at the top, fields for the student to fill in (Name, Date, Class, Candidate Number, etc.), often a barcode or ID box, and NO question text. Return true if this is clearly the cover page; false if this is a regular question/answer page or any other non-cover page.

4. CONFIDENCE — How confident are you in the handwriting answer above? Return an integer 0–10, where 10 means "absolutely certain", 5 means "I genuinely cannot decide", and 0 means "I have no idea". Lower the score when you see marginal/ambiguous marks (e.g. faint show-through that *might* be deliberate, partial doodles, smudges).

5. REASON — One short sentence (under 20 words) justifying the handwriting answer. Be specific: name what you see (or don't see) and where on the page.

   Good: `"Three lines of cursive ink in left margin, mid-page."` (specific, names location and form)
   Bad:  `"Yes, there is writing."` (generic, gives no detail)

Return JSON only, with this exact shape:
{"has_handwriting": <bool>, "page_number": <int|null>, "is_cover_page": <bool>, "confidence": <int 0..10>, "reason": "<one short sentence>"}
