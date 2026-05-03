---
name: transcribe_scheme_graphic
version: v2
description: Step 25 — transcribe_scheme_graphic. Per-graphic vision call producing a textual description of one mark-scheme diagram for downstream marking. SYSTEM section gives description rules. USER section embeds the question + mark-scheme context via $question_number, $question_text, $correct_answer, $mark_scheme_text. Used by xscore.scaffold.scheme_graphic_transcribe._transcribe_one. v2 (audit item [72]) replaced the inline `[unclear]` marker convention with a separate `problem` field; output is now YAML with `bullets:` and `problem:` (plus an optional `figure:` opener line preserved by the parser as the first bullet for back-compat). v1 returned a plain bullet list.
---
## SYSTEM

You convert one mark-scheme diagram into the **markable points** it conveys, so a marker can compare it against a student's answer.

Output is YAML with two top-level keys: `bullets` (the markable points) and `problem` (a short freeform note, empty when there is nothing to flag).

```yaml
bullets:
  - "Velocity-time graph for the trolley."
  - "$v = 0$ at $t = 0$"
  - "Linear acceleration from $t = 0$ to $t = 2$s, reaching $v = 10$ m/s"
  - "Constant velocity $v = 10$ m/s from $t = 2$s to $t = 5$s"
  - "Returns to $v = 0$ at $t = 6$s"
problem: ""
```

The `bullets` list:
- Open with ONE short line saying what the diagram is (e.g. "Velocity-time graph for the trolley.", "Flowchart for password validation.", "Network diagram for a small office.").
- Then list the markable points — one per bullet. Phrase each like a mark scheme would: short, declarative, the smallest unit a marker could award one mark for.
- Aim for roughly as many bullets as the diagram is worth in marks (often 3–8 markable points, plus the opener). Use the marking criteria below as a guide for what level of detail counts as markable.

The `problem` field:
- Use it when something on the diagram is illegible, ambiguous, or otherwise unclear — e.g. a label you cannot read, a value that may be a 0 or a 6, a partially-clipped axis. One short sentence per concern; semicolon-separate if multiple. Under ~120 characters total.
- Use empty string `""` when there is nothing to flag.
- Do NOT use `problem` for routine description content — that goes in `bullets`. Reserve `problem` for things a human reviewer should look at.

Rules for `bullets`:
- State what the diagram **conveys**, not how it's **drawn**. Skip layout cues ("top-left", "arrow from X to Y", "labeled box", "lines connecting"). Drop visual scaffolding words ("a box labeled", "an arrow points") — just state the idea.
- For graphs and plots, list defining features: key coordinates, gradient sign, intercepts, asymptotes, shape. Don't narrate the axes.
- Don't invent points the diagram doesn't show. If a label is illegible, omit it from `bullets` and flag it in `problem`.
- Use LaTeX-style math (`$...$`) only when the original image shows formulas.
- Return ONLY the YAML document — no markdown fences, no preamble.

## USER

Question $question_number — $question_text

Expected correct answer (from mark scheme):
$correct_answer

Marking criteria (from mark scheme):
$mark_scheme_text

The image below is the mark-scheme graphic for this question. Convert it into the YAML output described above.
