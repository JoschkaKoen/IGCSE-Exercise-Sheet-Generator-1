---
name: exam_blank_detection
version: v2
description: Step 17 — exam_blank_detection. User-only prompt for the text-only LLM call that identifies blank pages in the empty exam PDF. Placeholders ${exam_pages_block}, ${num_pages}, ${page_word}, ${candidates} are pre-built by xscore.marking.blank_page_detection.find_blank_exam_pages. v2 names the response shape explicitly (`{"blank_pages": [<int>, ...]}`) and sharpens the BLANK-PAGE hint. Step number was 14 in earlier pipeline versions; current run-folder is `17_exam_blank_detection`.
---

You are analysing an empty exam paper.
Below is the printed text from each page.

${exam_pages_block}
Identify all BLANK pages. A blank page:
- May contain the words "BLANK PAGE" (the printed phrase "BLANK PAGE" is a strong signal — Cambridge papers print it on every truly blank page)
- Has NO exercise instructions or question text
- May have printed horizontal lines (writing lines for students) — these do NOT
  disqualify a page from being blank

The exam has ${num_pages} ${page_word} in total.
Return JSON only: `{"blank_pages": [<int>, <int>, ...]}`. Each integer must come from this list: ${candidates}.
