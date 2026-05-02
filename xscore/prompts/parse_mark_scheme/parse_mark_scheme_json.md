---
name: parse_mark_scheme_json
version: v3
description: Step 20 — parse_mark_scheme. Combined system + user prompt for mark-scheme extraction in JSON format. Placeholder $scaffold (Template syntax) holds the question scaffold inserted into the user prompt. JSON-string backslashes appear as \\ (two literal backslashes — needed because the AI emits them inside JSON strings). Body has `$v` in the LaTeX example; Template's safe_substitute leaves it literal as long as no `v=` is passed (only $scaffold is intended). v3 added a constraint forbidding the model from transcribing diagrams: no `\includegraphics`/`\graphicspath` and no verbal "Diagram text: …" criteria — diagrams are extracted by step 22 and inserted by the renderer. v2 added the MCQ-specific rule asking the model to put the mark scheme's explanation of the correct answer into a single `mark: 0` criteria entry — downstream code routes it into `Question.reasoning`. Used by xscore.scaffold.formats.json_format.JsonScaffoldFormat.
---
## SYSTEM

You are an expert at reading Cambridge IGCSE mark schemes. Extract marking criteria. Return JSON matching the response schema.

## USER

For each question in the scaffold below, fill in `correct_answer` and `criteria` based on the mark scheme.

$scaffold

- `correct_answer`: always a non-empty string — the model/expected answer. For multiple-choice: just the letter (e.g. "C"). For questions with a single definitive answer: that answer (e.g. "930D", "00001111"). For "any N from" / open-ended questions: write a brief sample answer derived from the criteria (e.g. "Actuator, Printer, Speaker" or "Any three from: A, B, C"). Never leave this empty or null.
- `criteria`: list of {mark, criterion} — extract the COMPLETE marking scheme text.
- For multiple-choice questions: put the mark scheme's explanation of the correct answer (typically a short bulleted breakdown) into a single `criteria` entry with `mark: 0` (the mark belongs to the `correct_answer` letter, not the explanation). Format the explanation as a LaTeX itemize list, e.g. "\\begin{itemize}\\item Option C is correct because ...\\item Option A is wrong because ...\\end{itemize}". Use `criteria: []` only when the mark scheme has no explanation.

LaTeX in criterion strings: use \\ for backslash in JSON strings.
Examples: "\\textbf{word}", "$v = 2\\pi r / T$"

When a question's mark scheme contains a diagram, figure, or graph, do NOT transcribe it. Do not emit \\includegraphics, \\graphicspath, or any image command, and do not produce criteria that verbally describe the diagram (e.g. "Diagram text: …", "The diagram demonstrates: bots send requests…"). Diagrams are extracted separately and inserted alongside the bullet criteria automatically. Include only criteria that are genuinely separate text in the printed mark scheme — list-style mark allocations, "MAX six" rules, accept/reject notes, and similar.

## CODE_FORMATTING

This exam contains code/pseudocode that must be rendered in monospace. In JSON `correct_answer` and `criterion` strings: inline code → "\\texttt{...}", multi-line code → "\\begin{alltt}...\\end{alltt}". Do not use \\textbf for code. Inside the alltt block, only escape { → \\{, } → \\}, backslash → \\textbackslash{}.
