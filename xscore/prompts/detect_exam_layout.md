---
name: detect_exam_layout
version: v2
description: Step 8 — detect_exam_layout. Combined system + user prompt for exam page layout detection. Returns rows/cols/reading_order JSON. Used by xscore.scaffold.scaffold_prompts. No substitutions. v2 added an explicit JSON-only directive and compressed the reading_order description. (Step number was 16 in earlier pipeline versions; current run-folder is `08_detect_exam_layout`.)
---
## SYSTEM

You are an expert at identifying exam paper printing layouts.

## USER

Look at this exam page image. Determine how many exam sub-pages are printed on this physical page and in what reading order they appear.

Return JSON only with this shape: `{"rows": <int>, "cols": <int>, "reading_order": [[<int>, <int>], ...]}`.

- `rows` — number of rows of sub-pages (1 or 2).
- `cols` — number of columns of sub-pages (1 or 2).
- `reading_order` — list of `[row, col]` pairs (1-based), in the order a reader scans them: left-to-right, then top-to-bottom.

Examples:

Standard single-page exam:
  {"rows":1,"cols":1,"reading_order":[[1,1]]}
Two-up landscape (left exam / right exam):
  {"rows":1,"cols":2,"reading_order":[[1,1],[1,2]]}
Four-up 2x2 grid, standard reading order:
  {"rows":2,"cols":2,"reading_order":[[1,1],[1,2],[2,1],[2,2]]}
