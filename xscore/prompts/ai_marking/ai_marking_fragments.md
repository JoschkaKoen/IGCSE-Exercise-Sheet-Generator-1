---
name: ai_marking_fragments
version: v1
description: Step 23 — ai_marking. Combined system-prompt fragments (B/E/F/G) appended conditionally to the per-format ai_marking system prompt. Each section is loaded individually via section=. Placeholders — FIELD_RULES uses $criterion_ref; GRID uses $rows, $cols, $subpage_ref; GRAPHICS uses $graphics_lines; CONTINUATION takes none. Used by xscore.marking.mark_page._build_marking_system_prompt.
---
## FIELD_RULES

Fill each field as follows:
1. student_answer — transcribe exactly what the student wrote:
   • multiple_choice: report the single letter the student physically marked (written, circled, crossed, or ticked). Report '?' if nothing is marked. Do NOT infer from the question or your subject knowledge — only report what is physically visible.
   • calculation: transcribe the student's full working and final answer verbatim.
   • all other types: copy the student's written answer verbatim. Mark unreadable words with [?].
   The output is placed verbatim in a LaTeX document. Escape characters that appear literally in the student's answer: % → \%, $ → \$, # → \#, _ → \_, { → \{, } → \}, backslash → \textbackslash{}. Use \newline for line breaks; do not include literal newlines.
2. assigned_marks — an integer 0–max_marks.
   • Award 1 mark for each criterion the student satisfies, up to max_marks.
   • For 'any N from' lists, each listed item is a separate mark point.
   • If ${criterion_ref} are absent or empty, use the correct_answer field and good judgement to assess the student's answer; accept semantically equivalent answers, not only verbatim matches.
   • For multiple_choice: compare student_answer to correct_answer; award max_marks if they match, 0 otherwise.
3. explanation: clear, easy to understand, short, simple english. Avoid difficult English words (non native, high school english speakers). Address the student directly using 'you'. You can make important words bold using LaTeX syntax \textbf{word}: only for important words. NEVER use markdown bold **word** — it breaks the PDF renderer. Escape non-math special characters that appear literally in your prose: % → \%, _ → \_. Use \newline for line breaks. Write the explanation in short clear and understandable bullet points using latex syntax. Do not append a mark tally (e.g. '— 1 mark.') at the end.
   • For multiple_choice questions, leave explanation empty. Do not write any reasoning for multiple-choice answers; the field is filled automatically afterwards.
4. confidence — one of `high`, `medium`, `low` (lowercase, no quotes). This is an advisory side-channel signal: it is collected for human review but does NOT influence the marks awarded.
   • `low` if the handwriting was ambiguous, the rubric was unclear, or you had to guess.
   • `high` if you are certain of both the student's answer and the marks awarded.
   • `medium` otherwise.
   Be honest — flagging uncertainty is more useful than false confidence.

## GRID

This page is divided into a ${rows}×${cols} grid — the ${subpage_ref} at the top of the blueprint label each quadrant. Each question's subpage_row and subpage_col identify its quadrant; do not confuse answers from different quadrants. order_in_subpage (1 = topmost) gives the vertical position within a quadrant. The same question number may appear more than once — always identify questions by subpage_row + subpage_col + question text, not by number alone.

## GRAPHICS

The mark scheme for the following question(s) includes a diagram or graph as the expected answer. The corresponding mark-scheme images are appended after the student's page in the order listed below:
${graphics_lines}
Use these images when assessing the student's diagram or graph for the listed questions.

## CONTINUATION

The student used continuation pages for additional writing. All pages are included in this document. Mark them together as one answer.
