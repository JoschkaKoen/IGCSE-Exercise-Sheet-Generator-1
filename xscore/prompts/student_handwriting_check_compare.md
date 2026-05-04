---
name: student_handwriting_check_compare
version: v1
description: Step 15 out-of-order recheck. Compares the scanned answer page against the printed empty-exam page that should be in this position. Returns a same-page yes/no plus confidence and a one-line reason. Used by xscore.marking.blank_page_detection._compare_pages when the primary call returns a page number that doesn't match the geometry-expected order.
---

You are looking at TWO images.

- Image 1 is one page from the printed empty exam. No handwriting.
- Image 2 is one page from a student's scanned answer paper. It HAS student handwriting on it. The handwriting is expected — ignore it.

Your job: are these the SAME printed page from the exam?

Compare the printed text, printed lines, printed question numbers, printed page number, and overall layout. Ignore the student's handwriting in image 2.

- Same printed content → `same_page: true`.
- Different printed content → `same_page: false`.

## Return JSON only — exact shape

```
{"same_page": <true | false>,
 "confidence": <int 0..10>,
 "reason": "<one short sentence>"}
```

`confidence`: 10 = certain, 5 = cannot decide.
`reason`: one short sentence (under 25 words) explaining your decision.

Return only the JSON object. No markdown, no extra text.
