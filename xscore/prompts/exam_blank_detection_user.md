---
name: exam_blank_detection_user
version: v1
description: Step 14 — text-only LLM call that identifies blank pages in the empty exam PDF. Placeholders ${exam_pages_block}, ${num_pages}, ${page_word}, ${candidates} are pre-built by xscore.marking.blank_page_detection.find_blank_exam_pages.
---

You are analysing an empty exam paper.
Below is the printed text from each page.

${exam_pages_block}
Identify all BLANK pages. A blank page:
- May contain the words "BLANK PAGE"
- Has NO exercise instructions or question text
- May have printed horizontal lines (writing lines for students) — these do NOT
  disqualify a page from being blank

The exam has ${num_pages} ${page_word} in total.
Return ONLY page numbers chosen from this list: ${candidates}.
Do not return any number that is not in that list.
