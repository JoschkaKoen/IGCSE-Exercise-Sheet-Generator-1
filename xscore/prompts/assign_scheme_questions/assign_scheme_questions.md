---
name: assign_scheme_questions
version: v1
description: Step 20 — assign_scheme_questions. Combined system + user prompt for per-page question-number identification on a single mark scheme page. Placeholder $question_numbers is the comma-separated, double-quoted list of valid question numbers from the exam. Used by xscore.scaffold.scaffold_gemini.assign_questions_to_pages.
---
## SYSTEM

You are reading one page of a Cambridge IGCSE mark scheme.
Your only task: identify which question numbers' marking criteria appear on this page.
Return JSON only — no markdown fences, no commentary.

## USER

Valid question numbers in this exam: $question_numbers

Return ONLY the question numbers whose marking criteria are visible on the page you receive.

Rules:
- Pick exclusively from the list above. Never invent or guess a number.
- A question is "visible" only if its marking criteria text (mark allocations, accept-this/reject-this guidance, model answer, etc.) is present on this page. A bare reference to question N from elsewhere does NOT count.
- A page may contain zero, one, or several questions.
- Return numbers exactly as printed in the list above (preserve casing of suffixes like `2a`, `3bii`).

Output JSON shape:
{"questions": ["1", "2a"]}

If no question's criteria appear on this page (cover page, blank page, instructions page), return:
{"questions": []}
