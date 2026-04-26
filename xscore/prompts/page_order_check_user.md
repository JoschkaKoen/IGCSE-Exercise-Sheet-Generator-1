---
name: page_order_check_user
version: v1
description: Step 13 — per-student page order check. Compares OCR'd scan pages against the empty exam baseline and flags out-of-order pages. Placeholders ${n_exam_pages}, ${exam_pages_block}, ${student_name}, ${student_pages_block} are pre-built by xscore.marking.page_order_check._build_per_student_prompt.
---

You are verifying that one student's scanned exam pages are in the correct order
and contain the correct content.

EMPTY EXAM PAGES (exact printed text, ${n_exam_pages} pages):
${exam_pages_block}

STUDENT SCAN — ${student_name} (OCR of printed text only, handwriting excluded):
${student_pages_block}
Your task: detect pages that are physically out of order in this student's scan.
A mismatch means the SEQUENCE of questions is wrong — e.g. the page at position 5
contains question 8's text when it should contain question 5's text.
To detect this: identify the question number(s) and question text visible on each page,
then check that the sequence in the student scan matches the sequence in the empty exam.
Both the reference text (PDF heuristic extraction) and the scan text (OCR) are imperfect —
focus on the identity and order of questions, not exact wording / spelling.
Ignore all student handwriting, answer variations, and minor OCR noise.
Only flag when a question clearly belongs to a different position in the exam.

Return JSON ONLY with this shape:
{"ok": <bool>, "issues": [{"position": <int>, "scan_page": <int>, "expected": <str>, "found": <str>, "detail": <str>}]}
When ok=true, issues MUST be []. When ok=false, list each problem page in issues.
