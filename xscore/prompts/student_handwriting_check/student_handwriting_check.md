---
name: student_handwriting_check
version: v3
description: Step 15 — student_handwriting_check. User-only prompt for the vision LLM call that checks one scanned exam page for student handwriting, reads the printed page number, AND identifies whether the page is the exam cover page. No substitutions. Used by xscore.marking.blank_page_detection._has_handwriting.
---

This is one page from a student's scanned exam. It may have printed horizontal writing lines, printed headers/footers, printed question text, and student handwriting. It may be the exam cover page, an answer page, or a fully blank page.

Answer THREE questions about THIS page:

1. STUDENT HANDWRITING — Is there any handwriting (ink written by the student) on this page? Ignore printed lines, printed text, question numbers, and page numbers. Faint marks that bleed through from the OTHER side of the paper (show-through) do NOT count — only report ink clearly and deliberately written on THIS side.

2. PRINTED PAGE NUMBER — What is the typeset page number printed on this page (usually in the very top margin or very bottom margin, often centered or in a corner)? Return only the integer. Do NOT return question numbers (e.g. "Q3", "Question 5"), section markers, or dates — only the page number. If formatted as "Page 5 of 30", return 5. If no printed page number is visible, or you cannot read it confidently, return null. Cover pages typically have no page number — return null in that case.

3. COVER PAGE — Is this the exam cover page? Cover pages typically have: the exam title at the top, fields for the student to fill in (Name, Date, Class, Candidate Number, etc.), often a barcode or ID box, and NO question text. Return true if this is clearly the cover page; false if this is a regular question/answer page or any other non-cover page.

Return JSON only, with this exact shape:
{"answer": <bool>, "page_number": <int|null>, "is_cover_page": <bool>}
