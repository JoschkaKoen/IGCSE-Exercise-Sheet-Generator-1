---
name: exam_blank_detection
version: v3
description: Step 17 — exam_blank_detection. User-only prompt for the text-only LLM call that identifies blank pages in the empty exam PDF. Placeholders ${exam_pages_block}, ${num_pages}, ${page_word}, ${candidates} are pre-built by xscore.marking.blank_page_detection.find_blank_exam_pages. v3 promoted "BLANK PAGE" to a definitive rule, added an example output, and required ascending order. v2 named the response shape explicitly. Step number was 14 in earlier pipeline versions; current run-folder is `17_exam_blank_detection`.
---

You are analysing an empty exam paper.
Below is the printed text from each page.

${exam_pages_block}
## Blank-page rule

A page is blank if and only if:
(a) its extracted text contains the literal string `BLANK PAGE`, OR
(b) the page has no extracted text at all.

Other text alongside `BLANK PAGE` (copyright, colophon, page number, paper code) does NOT change this — if `BLANK PAGE` is there, the page is blank.

Printed horizontal lines (writing lines for students) do NOT disqualify a page from being blank.

## Example

```
Page 1: cover (name fields, instructions)
Page 2–11: question text
Page 12: copyright text + "BLANK PAGE"
```
→ `{"blank_pages": [12]}`

## Output

The exam has ${num_pages} ${page_word} in total.
Return JSON only: `{"blank_pages": [<int>, <int>, ...]}`. Each integer must come from this list: ${candidates}. Return integers in ascending order.
