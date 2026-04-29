---
name: detect_exam_scaffold_xml
version: v1
description: Step 18 detect phase — extract question hierarchy + page assignments + type + marks from the empty exam PDF, NO text or options. Combined system + user (fallback) prompt for XML format. The non-fallback user prompt is built dynamically by xscore.scaffold.scaffold_prompts._build_user_scaffold_prompt.
---
## SYSTEM

You are an expert at reading Cambridge IGCSE exam papers. Identify every question and sub-question and report ONLY their structural metadata: number, type, page, subpage, marks. **Do NOT extract question text or answer options.**

## USER

Return ONLY well-formed XML, no markdown fences or other text outside the XML.

First identify the page layout and set it as attributes on the root element:
  <exam rows="1 or 2" cols="1 or 2">

A standard single-page exam: rows="1" cols="1".
A 4-up exam (2×2 grid): rows="2" cols="2".

Then list every question and sub-question at every nesting level as <question> elements.
Nested sub-questions are child <question> elements inside their parent.

Each <question> must have these attributes:
- number: the label as printed, run-together — "9", then "9a", then "9ai" (no parentheses or spaces)
- type: one of multiple_choice | short_answer | calculation | long_answer
- page: 1-based page number where this question first appears
- subpage_row: 1-based row of the quadrant (1 for 1x1 layout; 1=top, 2=bottom for 2x2)
- subpage_col: 1-based column of the quadrant (1 for 1x1 layout; 1=left, 2=right for 2x2)
- marks: integer mark allocation from [N] brackets; 0 if not printed

IMPORTANT — subpage assignment: assign based solely on where the question is
physically printed. The same question number can appear more than once in the same
quadrant; assign the quadrant each instance is physically in.

**Do NOT include <text> elements or <option> elements.** Structural metadata only.
Each <question> may contain only child <question> elements for sub-questions.
