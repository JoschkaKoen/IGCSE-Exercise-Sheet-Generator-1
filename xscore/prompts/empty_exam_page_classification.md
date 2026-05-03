---
name: empty_exam_page_classification
version: v2
description: Step 14 phase A — empty_exam_page_classification. User-only prompt for the vision LLM call that classifies ONE page from the EMPTY exam paper into one of {cover page, instruction page, question page, blank page, writing space page} and reads its printed page number. Per-page parallel calls; with Gemini the page is delivered as native inline PDF (one-page slice), with non-Gemini providers as a rasterized JPEG. No substitutions. Used by xscore.marking.blank_page_detection.classify_empty_exam_pages. The classification catalog produced here drives phase B's closed-vocabulary scan-page matcher. v2 added the `writing space page` type.
---

This is ONE page from an EMPTY (unfilled) exam paper. There is no student writing on it — only the printed material. Classify the page and read its printed page number.

## Page types

Pick exactly one:

- `cover page` — exam title at the top, fields for the student to fill in (Name, Date, Class, Candidate Number, etc.), and NO question text. May also have a barcode or ID box. Cover pages typically have NO printed page number.
- `instruction page` — no name field, no question text. General instructions, formula sheet, candidate notice, periodic table, or similar reference material.
- `question page` — has printed question text (e.g. "1.", "Q3", "Question 5", or a sub-part like "(a)").
- `blank page` — contains the words "BLANK PAGE" prominently (typically a deliberately-left-blank page with that exact label).
- `writing space page` — the page is dominated by printed horizontal writing lines (solid or dotted) intended for the student to write on, AND has no printed question text, no question label ("1.", "Q3", "(a)", etc.), no formula sheet content, and no "BLANK PAGE" label. Short printed scaffolding such as a page number, "Turn over", "Section B", "Working space", "Continue your answer here", or a continuation note pointing back to a prior question (e.g. "(continued from Q3)") does NOT disqualify a page from being a writing space page. The defining feature is: lines for writing + absence of new question content.

If a page fits more than one category, apply this ranking in order:
- If it has even ONE printed question label or stem, it is `question page` — even if 90% of the page is empty lines underneath.
- If it has the literal "BLANK PAGE" label, it is `blank page`.
- If it is dominated by writing lines and has no new question content (per the bullet above), it is `writing space page`.
- If it has both name fields and ruled lines (rare title-page hybrid), it is `cover page`.
- Otherwise pick the dominant category and explain in `problem`.

## Page number

Read the typeset page number printed in the margin (usually top-center, top-corner, bottom-center, or bottom-corner). Examples:
- "5" → return `5`
- "Page 5 of 30" → return `5`
- Any digits visible only as part of a question label (e.g. "Q3") or as part of a date / paper code — DO NOT return those.

If no printed page number is visible, return `null`. **Cover pages typically have no page number — `null` is the expected answer there.**

## Confidence

Two confidences, integer 0..10 each:

- `confidence_page_type` — how sure you are about which of the five types this page is. Lower when the page has features of two categories (e.g. an instruction page that also has a small question stem).
- `confidence_page_number` — how sure you are about the page number. Lower when the digit is faint, partially cropped, or if you returned `null` because nothing was clearly visible.

10 = absolutely certain. 5 = cannot decide. 0 = no idea.

## Problem

Fill `problem` with one short sentence (under 25 words) when ANY of:
- `confidence_page_type < 7`
- `confidence_page_number < 7`
- a field could not be determined (e.g. you returned `null` for `page_number` and weren't confident about why)

**Exception**: a `cover page` with `page_number: null` is the expected, normal case — leave `problem` empty in that situation. Don't flag it.

If neither condition triggers, leave `problem` as the empty string `""`.

## Return JSON only — exact shape

```
{"page_type": "cover page" | "instruction page" | "question page" | "blank page" | "writing space page",
 "page_number": <int> | null,
 "confidence_page_type": <int 0..10>,
 "confidence_page_number": <int 0..10>,
 "problem": "<one short sentence or empty string>"}
```
