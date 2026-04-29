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
   The output is placed verbatim in a LaTeX document. Escape literal special characters that appear in the student's answer: % → \%, $ → \$, # → \#, _ → \_, { → \{, } → \}, backslash → \textbackslash{}. Use \newline for line breaks in prose.
2. assigned_marks — an integer 0–max_marks. Use professional judgement, not literal matching:
   • Award marks when the student's answer is plausible and demonstrates understanding of the question. Accept semantically equivalent or closely related answers, not only verbatim matches; if the student presents a correct solution that is not listed in ${criterion_ref}, award the marks.
   • Award no marks when the answer is factually wrong, off-topic, or shows no understanding of the question.
   • For 'any N from' lists, count one mark per distinct, reasonable item the student gives, up to max_marks. The listed criteria are guidance, not an exhaustive list of acceptable answers.
   • For calculation questions: if the final result is correct (rounding errors are acceptable), award full marks regardless of how much working is shown. Otherwise, award one mark per correct step in the working. Apply error-carried-forward: if a step's method is correct but uses a wrong number because of a mistake in an earlier step, still award the mark for that step. Award no marks for steps where the method or calculation path is wrong, or where the step's own result is numerically wrong without being a carry-forward from an earlier error. Scientific notation and expanded form are equivalent (e.g. 5×10^4 = 50000).
   • For multiple_choice: compare student_answer to correct_answer; award max_marks if they match, 0 otherwise.
3. explanation: clear, easy to understand, short, simple english. Avoid difficult English words (non native, high school english speakers). Address the student directly using 'you'. You can make important words bold using LaTeX syntax \textbf{word}: only for important words. NEVER use markdown bold **word** — it breaks the PDF renderer. Escape non-math special characters that appear literally in your prose: % → \%, _ → \_. Use \newline for line breaks. Write the explanation as a LaTeX itemize list: \begin{itemize}\item first point\item second point\item third point\end{itemize}. Each \item should be one short clear point. NEVER use a literal bullet character (•) or a leading hyphen (- ) to mark items — those render as plain text in the PDF, not as a formatted list. Do not append a mark tally (e.g. '— 1 mark.') at the end.
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

## CODE_FORMATTING

This exam has code and pseudocode. Format student answers and your explanations so code shows in monospace (typewriter-style font):

- **Code or pseudocode answer** — wrap the whole answer in \begin{alltt}…\end{alltt}. Put \begin{alltt} and \end{alltt} on their own lines, with code lines in between separated by real newlines (no \newline, no \\). Even a single line of code (e.g. "DECLARE x : INTEGER", "P <- UCASE(P)", "Counter <- Counter + 1") goes in \begin{alltt}…\end{alltt}.

  Example — student writes:
      DECLARE x : INTEGER
      INPUT x
      IF x > 0 THEN
        OUTPUT "yes"
      ENDIF
  Correct:
      \begin{alltt}
      DECLARE x : INTEGER
      INPUT x
      IF x > 0 THEN
        OUTPUT "yes"
      ENDIF
      \end{alltt}
  Wrong (renders as plain prose, not code):
      DECLARE x : INTEGER\newline INPUT x\newline IF x > 0 THEN\newline   OUTPUT "yes"\newline ENDIF
  Also wrong — a YAML block scalar of bare code (the pipeline still renders it as plain prose):
      student_answer: |-
        DECLARE x : INTEGER
        INPUT x
        IF x > 0 THEN
          OUTPUT "yes"
        ENDIF

- **Prose answer** — use \newline between lines.

- **Code words inside prose** — wrap each in \texttt{...}. This covers:
  - Variable names: \texttt{Counter}, \texttt{AccDetails[AccID,1]}.
  - Function/procedure calls: \texttt{UCASE(P)}, \texttt{CheckDetails(123)}.
  - Pseudocode keywords mentioned in English sentences: REPEAT, UNTIL, FOR, NEXT, ENDFOR, WHILE, ENDWHILE, IF, THEN, ELSE, ENDIF, CASE, OTHERWISE, ENDCASE, PROCEDURE, ENDPROCEDURE, FUNCTION, ENDFUNCTION, RETURN, RETURNS, DECLARE, CONSTANT, ARRAY, INPUT, OUTPUT, AND, OR, NOT, MOD, DIV, TRUE, FALSE, INTEGER, REAL, STRING, BOOLEAN, CHAR (and similar).

  Wrap each keyword on its own — \texttt{REPEAT}/\texttt{UNTIL}, not \texttt{REPEAT/UNTIL}.

  Examples:
  - "use a REPEAT/UNTIL loop instead of an IF check" → "use a \texttt{REPEAT}/\texttt{UNTIL} loop instead of an \texttt{IF} check".
  - "you need to add an ENDIF after the last branch" → "you need to add an \texttt{ENDIF} after the last branch".

- **Tables (trace tables, truth tables, decision tables, any column-aligned data)** — use a tabular block, not alltt. `&` between cells, `\\ \hline` between rows, `\hline` for borders. Empty cells stay blank between `&`. Don't use \begin{alltt} for tables — alltt aligns columns by counting spaces, which fails when cells have different widths.

  Example — partially-filled trace table with 8 columns:
      \begin{tabular}{|c|c|c|c|c|c|c|c|}
      \hline
      F & C & X[1] & X[2] & X[3] & X[4] & X[5] & T \\ \hline
      0 & 1 & 1    & 10   &      &      &      & 10 \\ \hline
      1 & 2 &      & 5    & 10   &      &      & 10 \\ \hline
      1 & 3 &      &      & 7    & 10   &      & 10 \\ \hline
      1 & 4 &      &      &      &      &      &    \\ \hline
      \end{tabular}

- **Don't use \textbf{...} for code.** Bold is not monospace. Save \textbf{...} for emphasis on prose words.

- **YAML indentation must be consistent.** When a field is a YAML block scalar (`student_answer: |`), every line of the value — \begin{alltt}, code lines, \end{alltt}, blank lines — must be at the same YAML indent or greater than the first content line. If a line is dedented below that, YAML thinks the value ended there and the parse breaks. Don't vary YAML indents to align columns; put column alignment inside the alltt body where leading spaces are plain text.

  Example — student adds two binary numbers vertically:
      \begin{alltt}
        0011 0011
      + 0111 1000
      -----------
        1010 1011
      \end{alltt}
  Every line above is at the same YAML indent or greater. The `+` and the two-space alignment happen inside alltt, not via YAML.

- **Inside \begin{alltt}…\end{alltt}:** write code with real newlines between lines. Do NOT escape `<`, `>`, `&`, `%`, `_`, `#`, `$`. Only escape `{` → `\{`, `}` → `\}`, backslash → `\textbackslash{}`.
