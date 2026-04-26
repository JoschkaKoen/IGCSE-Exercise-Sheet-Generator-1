---
name: ai_marking_graphics
version: v1
description: Step 23 — ai_marking. System prompt fragment for mark-scheme graphics (Section F). Appended only when one or more questions on the page have a diagram or graph in the mark scheme. ${graphics_lines} is a pre-formatted multi-line list of "  • Question Q expected answer → image" entries. Used by xscore.marking.mark_page._build_marking_system_prompt.
---
The mark scheme for the following question(s) includes a diagram or graph as the expected answer. The corresponding mark-scheme images are appended after the student's page in the order listed below:
${graphics_lines}
Use these images when assessing the student's diagram or graph for the listed questions.
