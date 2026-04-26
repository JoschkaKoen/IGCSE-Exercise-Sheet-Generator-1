---
name: parse_grading_instructions
version: v1
description: Step 1 — parse_grading_instructions. System-only prompt that converts a natural-language grading instruction into a structured TaskInstruction JSON object. No substitutions. Used by xscore.marking.parse_instruction.parse_prompt.
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
  "reuse_cache": false,
  "curved_grade_override": null,
  "curved_grade_visible": null
}

task_type: count_marks=tally red teacher marks; check_mc=MC only; check_answers=all types.
student_filter.mode: all=default; specific=named students; first_n=first N (set n). names=list.
dpi: 400 default; 300 if "fast"/"quick"; 600 if "high quality"/"accurate".
folder_hint: short name for fuzzy folder match. folder_path: absolute or ~-relative path; set only when user gives an explicit path; else null. Prefer folder_path when both apply.
Examples: "from ~/Desktop/exams/physics" → folder_path "~/Desktop/exams/physics", folder_hint null; "Space Physics test" → folder_hint "Space Physics", folder_path null.
force_clean_scan: true=ignore cache, re-clean ("re-clean", "force deskew").
no_report: true=skip PDF ("terminal only", "no report").
from_step: integer step number to resume from ("from step 14", "resume from step 13", "rerun from step 15"); null otherwise.
reuse_cache: true=use cached AI marking responses from previous identical runs ("reuse cache", "use cache", "from cache"); false otherwise. Default false.
curved_grade_override: integer 0–100 to override the grade-curve target ("curve at 70", "target 75%", "curve to 80"); null if the user did not specify a target.
curved_grade_visible: false if the user wants the curved percentage hidden from per-student PDFs ("hide curve from students", "don't show curve on student reports", "no curve on student PDFs"); true if the user explicitly asks to show it; null if the user did not mention it.
