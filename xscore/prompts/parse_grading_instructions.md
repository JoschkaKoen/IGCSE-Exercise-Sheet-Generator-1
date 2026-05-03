---
name: parse_grading_instructions
version: v3
description: Step 1 — parse_grading_instructions. System-only prompt that converts a natural-language grading instruction into a structured TaskInstruction JSON object. No substitutions. Used by xscore.marking.parse_instruction.parse_prompt. v3 standardised every field's `Default:` line, added an ambiguous-prompt fallback, sharpened the folder_hint definition, reordered field rules to match the schema, and appended worked examples. v2 named `check_answers` as the default task_type.
---
Convert the grading instruction to JSON. Return ONLY the JSON, no explanation.

{
  "task_type": "count_marks|check_mc|check_answers",
  "student_filter": {"mode": "all|specific|first_n", "names": [], "n": 0},
  "dpi": 400,
  "folder_hint": null,
  "folder_path": null,
  "force_clean_scan": false,
  "no_report": false,
  "from_step": null,
  "stop_after": null,
  "reuse_cache": false,
  "curved_grade_override": null,
  "curved_grade_visible": null
}

Field rules — listed in the same order as the schema above. Each rule lists triggers (in backticks), then the **Default:** value to use when no trigger matches.

- **task_type** — `count_marks` = tally red teacher marks; `check_mc` = MC only; `check_answers` = all types.
  - Use `count_marks` only when the user explicitly says `count marks` / `tally red marks`.
  - Use `check_mc` only when the user explicitly says `multiple choice only`.
  - **Default:** `check_answers`.
- **student_filter** — `{mode, names, n}`.
  - `mode: specific` when the user names students; populate `names` with the list.
  - `mode: first_n` for "first N students"; populate `n`.
  - **Default:** `{mode: all, names: [], n: 0}`.
- **dpi** — 300 if `fast` / `quick`; 600 if `high quality` / `accurate`.
  - **Default:** `400`.
- **folder_hint** — the noun phrase that names the exam (typically the words before `exam` / `test`, or the only proper noun in the prompt). Used for fuzzy folder match when no explicit path is given.
  - **Default:** `null`.
- **folder_path** — absolute or `~`-relative path. Set ONLY when the user gives an explicit path. Prefer `folder_path` over `folder_hint` when both apply.
  - **Default:** `null`.
- **force_clean_scan** — `true` when the user says `re-clean` / `force deskew`.
  - **Default:** `false`.
- **no_report** — `true` when the user says `terminal only` / `no report`.
  - **Default:** `false`.
- **from_step** — integer step to resume from (`from step 14`, `resume from step 13`, `rerun from step 15`).
  - **Default:** `null`.
- **stop_after** — integer step to stop after (`stop after step 5`, `only run steps 1-5`, `halt at step 7`, `first 5 steps`, `only the first 13 steps`). Distinct from `from_step`: `from_step` controls where the pipeline starts; `stop_after` controls where it stops. They can be combined — see worked example below.
  - **Default:** `null`.
- **reuse_cache** — `true` when the user says `reuse cache` / `use cache` / `from cache`.
  - **Default:** `false`.
- **curved_grade_override** — integer 0–100 to override the grade-curve target (`curve at 70`, `target 75%`, `curve to 80`).
  - **Default:** `null`.
- **curved_grade_visible** — `false` if the user wants the curved percentage hidden from per-student PDFs (`hide curve from students`, `don't show curve on student reports`, `no curve on student PDFs`); `true` if the user explicitly asks to show it.
  - **Default:** `null`.

## Ambiguous prompts

If the user's intent is ambiguous (e.g. `rerun the marking step` without naming a step number), set the affected fields to their **Default:** values rather than guessing. Better to fall back to defaults than to invent an interpretation.

## Worked examples

Input: `grade '/Users/me/Desktop/exams/s23 12' at 300 dpi`
Output:
```json
{"task_type": "check_answers", "student_filter": {"mode": "all", "names": [], "n": 0}, "dpi": 300, "folder_hint": null, "folder_path": "/Users/me/Desktop/exams/s23 12", "force_clean_scan": false, "no_report": false, "from_step": null, "stop_after": null, "reuse_cache": false, "curved_grade_override": null, "curved_grade_visible": null}
```

Input: `count marks for first 5 students of Space Physics test, only run steps 5-10, reuse cache`
Output:
```json
{"task_type": "count_marks", "student_filter": {"mode": "first_n", "names": [], "n": 5}, "dpi": 400, "folder_hint": "Space Physics", "folder_path": null, "force_clean_scan": false, "no_report": false, "from_step": 5, "stop_after": 10, "reuse_cache": true, "curved_grade_override": null, "curved_grade_visible": null}
```
