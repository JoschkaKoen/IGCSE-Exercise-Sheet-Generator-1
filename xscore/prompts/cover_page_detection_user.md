---
name: cover_page_detection_user
version: v1
description: Steps 9 & 10 — cover_page_empty_exam and cover_page_scan. User prompt that classifies one exam page as COVER PAGE or QUESTION PAGE, given its OCR/printed text. Placeholder $text holds the printed page text (Template syntax). Used by xscore.preprocessing.assign_pages_to_students.is_cover_page (step 10) AND check_cover_page_text (step 9).
---
You are classifying an exam page as either a COVER PAGE or a QUESTION PAGE.

## Cover page
A cover page does NOT contain any exam questions. It may contain any of the following:
- Exam title, subject name, paper code, date, or duration
- Barcode, document reference numbers, or publisher information
- General instructions to students, such as:
    - "Answer all questions"
    - "Write your answers in the spaces provided"
    - "Use a black or dark blue pen"
    - "Do not use a calculator"
- Exam information such as total marks, mark allocation notes, or permitted materials
- Student identification fields (name, candidate number, centre number, class, or date)

## Question page
A question page contains at least one actual exam question — a numbered or lettered prompt
that asks the student to do something specific, such as:
- "Describe how…"
- "Calculate the…"
- "State two reasons why…"
- "Give one example of…"
- A diagram or table with numbered sub-questions

## Important disambiguation
Phrases such as "Answer all questions" or "Write your answer to each question in the space
provided" are general student instructions that appear on cover pages. They do NOT indicate
that exam questions are present on this page.

## Examples

Cover page → true:
  "Computer Science 0478/12 — 1 hour 45 minutes.
   Write your name, centre number and candidate number in the boxes at the top of the page.
   Answer all questions. The total mark for this paper is 75."

Question page → false:
  "1 (a) Describe two advantages of using a database rather than a flat file. [4]
   (b) State what is meant by a primary key. [1]"

## Page text

--- BEGIN PAGE TEXT ---
$text
--- END PAGE TEXT ---

Is this a cover page (no actual exam questions present)?
