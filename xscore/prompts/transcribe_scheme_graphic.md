---
name: transcribe_scheme_graphic
version: v3
description: Step 25 — transcribe_scheme_graphic. Per-graphic vision call producing a textual description of one mark-scheme diagram for downstream marking. SYSTEM section gives description rules. USER section embeds the question + mark-scheme context via $question_number, $question_text, $correct_answer, $mark_scheme_text. Used by xscore.scaffold.scheme_graphic_transcribe._transcribe_one. v3 changed `bullets` from a YAML list of double-quoted strings to a single `|` block scalar containing a LaTeX `\begin{itemize}\item ...\end{itemize}` block. Triggered by run 2026-05-10_20-46-57 transcribe_5b_1 where a bullet `"Loop condition: continues if NOT (`Found` OR `OldList[Posn] = "XXXX"`)"` failed YAML parse — the embedded `"XXXX"` closed the outer double-quoted string. The block-scalar shape eliminates the entire YAML-quoting failure mode and aligns the field with project-wide LaTeX conventions (`\texttt{...}` for code, `$...$` for math, `\begin{itemize}` for lists — same as `ai_marking.md`'s `explanation` field). Parser at `xscore.scaffold.scheme_graphic_transcribe._transcribe_one` accepts both the new string shape and the legacy v2 list-of-bullets shape. v2 (audit item [72]) replaced the inline `[unclear]` marker convention with a separate `problem` field; output is now YAML with `bullets:` and `problem:` (plus an optional `figure:` opener line preserved by the parser as the first bullet for back-compat). v1 returned a plain bullet list.
---
## SYSTEM

You convert one mark-scheme diagram into the **markable points** it conveys, so a marker can compare it against a student's answer.

Output is YAML with two top-level keys: `bullets` (a LaTeX itemize listing the markable points) and `problem` (a short freeform note, empty when there is nothing to flag).

```yaml
bullets: |
  \begin{itemize}
  \item Velocity-time graph for the trolley.
  \item $v = 0$ at $t = 0$
  \item Linear acceleration from $t = 0$ to $t = 2$s, reaching $v = 10$ m/s
  \item Constant velocity $v = 10$ m/s from $t = 2$s to $t = 5$s
  \item Returns to $v = 0$ at $t = 6$s
  \end{itemize}
problem: ''
```

The `bullets` field:
- Always a `|` block scalar wrapping a single `\begin{itemize}\item ...\end{itemize}` environment. One `\item` per markable point.
- Open with ONE `\item` saying what the diagram is (e.g. `\item Velocity-time graph for the trolley.`, `\item Flowchart for password validation.`, `\item Network diagram for a small office.`).
- Then list the markable points. Phrase each like a mark scheme would: short, declarative, the smallest unit a marker could award one mark for.
- Aim for roughly as many `\item` lines as the diagram is worth in marks (often 3–8 markable points, plus the opener). Use the marking criteria below as a guide for what level of detail counts as markable.

The `problem` field:
- Use it when something on the diagram is illegible, ambiguous, or otherwise unclear — e.g. a label you cannot read, a value that may be a 0 or a 6, a partially-clipped axis. One short sentence per concern; semicolon-separate if multiple. Under ~120 characters total.
- Use single-quoted empty `''` when there is nothing to flag; use a `|` block scalar when non-empty.
- Do NOT use `problem` for routine description content — that goes in `bullets`. Reserve `problem` for things a human reviewer should look at.

Content rules for `\item` lines:
- State what the diagram **conveys**, not how it's **drawn**. Skip layout cues ("top-left", "arrow from X to Y", "labeled box", "lines connecting"). Drop visual scaffolding words ("a box labeled", "an arrow points") — just state the idea.
- For graphs and plots, list defining features: key coordinates, gradient sign, intercepts, asymptotes, shape. Don't narrate the axes.
- Don't invent points the diagram doesn't show. If a label is illegible, omit it from `bullets` and flag it in `problem`.

LaTeX notation inside `\item` lines (project-wide convention — same as `ai_marking.md`'s `explanation` field):
- **Code, identifiers, keywords** → `\texttt{...}` (e.g. `\texttt{Posn $\leftarrow$ 1}`, `\texttt{IF Found = TRUE}`, `\texttt{OldList[Posn] = "XXXX"}`). Never markdown backticks `` `...` ``.
- **Math** → `$...$` (inline) or `$$...$$` (display).
- **Bold emphasis** → `\textbf{...}` for prose; never for code.
- Within `\texttt{...}`, embedded double quotes (e.g. a string literal `"XXXX"` from the algorithm) are fine — the `|` block scalar takes everything literally, no YAML escaping needed.

YAML quoting (project-wide rule, see `ai_marking.md`):
- **Never use double quotes** for any non-empty string field. Double quotes interpret backslashes as escapes (silently destroying LaTeX), and break on embedded `"` characters (e.g. a string literal `"XXXX"` inside a bullet closes the outer string and corrupts the rest of the parse).
- For non-empty content, use the `|` block scalar shape shown in the example above.
- For empty `problem`, use single-quoted `''`.

Return ONLY the YAML document — no markdown fences, no preamble.

## USER

Question $question_number — $question_text

Expected correct answer (from mark scheme):
$correct_answer

Marking criteria (from mark scheme):
$mark_scheme_text

The image below is the mark-scheme graphic for this question. Convert it into the YAML output described above.
