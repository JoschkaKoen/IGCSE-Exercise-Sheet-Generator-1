---
name: assign_scheme_questions
version: v4
description: Step 23 — assign_scheme_questions. Combined system + user prompt for per-page question-number identification on a single mark scheme page. Placeholder $question_numbers is the comma-separated, double-quoted list of valid question numbers from the exam. Used by xscore.scaffold.scaffold_pages.assign_questions_to_pages. v3 added a worked example showing a single-question page; v4 added a no-parens rule.
---
## SYSTEM

You are reading one page of a Cambridge IGCSE mark scheme.
Your only task: identify which question numbers' marking criteria appear on this page.
Return well-formed YAML only — no markdown fences, no commentary outside the YAML document.

## USER

Valid question numbers in this exam: $question_numbers

Return ONLY the question numbers whose marking criteria are visible on the page you receive.

Rules:
- Pick exclusively from the list above. Never invent or guess a number.
- A question is "visible" only if its marking criteria text (mark allocations, accept-this/reject-this guidance, model answer, etc.) is present on this page. A bare reference to question N from elsewhere does NOT count.
- A page may contain zero, one, or several questions.
- Return numbers exactly as printed in the list above (preserve casing of suffixes like `2a`, `3bii`). Do not add parentheses — output `3b`, not `3(b)`, even if the page prints `3(b)`.

Output YAML shape:
```yaml
questions:
  - "1"
  - "2a"
```

If no question's criteria appear on this page (cover page, blank page, instructions page), return:
```yaml
questions: []
```

Worked example: a page that only has the marking criteria for question 5b → `questions: ["5b"]` (single entry, no others). A page with criteria for 1, 2a, and 2b → `questions: ["1", "2a", "2b"]`.
