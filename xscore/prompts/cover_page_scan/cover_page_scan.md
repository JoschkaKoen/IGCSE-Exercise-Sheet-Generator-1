---
name: cover_page_scan
version: v4
description: Step 10 (empty exam) AND step 11 (student scan) cover-page detection. Same template, two call sites. Split into ## SYSTEM (rules) and ## USER (per-call page text via $text). Used by xscore.preprocessing.cover_detection.is_cover_page (step 11) AND check_cover_page_text (step 10). v4 split SYSTEM/USER and refactored call sites to pass them as separate roles (Gemini system_instruction; OpenAI-compat two messages). v3 condensed the cover-page bullet list and expanded the disambiguation phrase list. v2 named the response shape explicitly.
---
## SYSTEM

You are classifying an exam page as either a COVER PAGE or a QUESTION PAGE.

## Cover page
A cover page does NOT contain any exam questions. It may contain:
- Identifying info: exam title, subject, paper code, date, duration, barcode, publisher info, total-marks notice.
- Rubric: general instructions to candidates (see disambiguation below).
- Student-fillable fields: name, candidate number, centre number, class, date.

## Question page
A question page contains at least one actual exam question — a numbered or lettered prompt
that asks the student to do something specific, such as:
- "Describe how…"
- "Calculate the…"
- "State two reasons why…"
- "Give one example of…"
- A diagram or table with numbered sub-questions

## Important disambiguation

These phrases appear on cover pages, not on question pages — even though they sound like instructions to do something:
- "Answer all questions"
- "Write your answer to each question in the space provided"
- "Use a black or dark blue pen" / "You may use an HB pencil for any diagrams or graphs"
- "Do not use an erasable pen or correction fluid" / "Do not write on any bar codes"
- "Calculators must not be used in this paper" / "No additional materials are needed"
- "The total mark for this paper is N" / "The number of marks for each question is shown in brackets"

These are RUBRIC, not exam questions. They do not turn the page into a question page.

### Examples

Cover page → true:
  "Computer Science 0478/12 — 1 hour 45 minutes.
   Write your name, centre number and candidate number in the boxes at the top of the page.
   Answer all questions. The total mark for this paper is 75."

Question page → false:
  "1 (a) Describe two advantages of using a database rather than a flat file. [4]
   (b) State what is meant by a primary key. [1]"

Return JSON only: `{"is_cover": true|false}`.

## USER

--- BEGIN PAGE TEXT ---
$text
--- END PAGE TEXT ---

Is this a cover page (no actual exam questions present)?
