---
name: cover_page_scan
version: v5
description: Step 10 (empty exam) AND step 11 (student scan) cover-page detection. Same template, two call sites. Split into ## SYSTEM (rules) and ## USER (per-call page text via $text). Used by xscore.preprocessing.cover_detection.is_cover_page (step 11) AND check_cover_page_text (step 10). v5 (audit item [28]/[30]) replaced the boolean `is_cover` output with the three-way enum `page_type` (cover / instructions / question). The downstream parser maps `cover` AND `instructions` to True for the existing boolean call site (alias-then-flip migration). v4 split SYSTEM/USER and refactored call sites to pass them as separate roles. v3 condensed bullet lists and expanded disambiguation. v2 named the response shape explicitly.
---
## SYSTEM

You are classifying an exam page into one of three categories:

- **`cover`** — the exam's front cover. Contains student-fillable fields (name, candidate number, centre number, class, date) AND no exam questions. Usually has the exam title, subject, paper code, date, duration, barcode.
- **`instructions`** — a rubric / general-guidance page with NO student-fillable name fields and NO exam questions. Examples: a standalone "Instructions to candidates" page that lists rules without any name boxes.
- **`question`** — has at least one actual exam question (a numbered or lettered prompt that asks the student to do something specific).

## How to tell them apart

A `cover` page has the COMBINATION: identifying info + name fields + no questions.

An `instructions` page has rubric text but neither name fields nor questions.

A `question` page has at least one of these — anything else falls through to `cover` or `instructions`:
- "Describe how…"
- "Calculate the…"
- "State two reasons why…"
- "Give one example of…"
- A diagram or table with numbered sub-questions

## Important disambiguation

These phrases appear on cover/instructions pages, not on question pages — even though they sound like instructions to do something:
- "Answer all questions"
- "Write your answer to each question in the space provided"
- "Use a black or dark blue pen" / "You may use an HB pencil for any diagrams or graphs"
- "Do not use an erasable pen or correction fluid" / "Do not write on any bar codes"
- "Calculators must not be used in this paper" / "No additional materials are needed"
- "The total mark for this paper is N" / "The number of marks for each question is shown in brackets"

These are RUBRIC, not exam questions. They do not turn the page into a `question` page.

### Examples

Cover page → `page_type: cover`:
  "Computer Science 0478/12 — 1 hour 45 minutes.
   Write your name, centre number and candidate number in the boxes at the top of the page.
   Answer all questions. The total mark for this paper is 75."
  (Has name/candidate fields → `cover`.)

Instructions page → `page_type: instructions`:
  "Instructions to candidates: Read all questions carefully before answering.
   Show all working. Calculators are permitted."
  (Rubric only, no name fields, no questions → `instructions`.)

Question page → `page_type: question`:
  "1 (a) Describe two advantages of using a database rather than a flat file. [4]
   (b) State what is meant by a primary key. [1]"

Return JSON only: `{"page_type": "cover" | "instructions" | "question"}`.

## USER

--- BEGIN PAGE TEXT ---
$text
--- END PAGE TEXT ---

What is this page's `page_type`?
