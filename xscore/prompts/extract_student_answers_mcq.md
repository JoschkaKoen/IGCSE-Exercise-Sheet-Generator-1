---
name: extract_student_answers_mcq
version: v2
description: Step 28 — extract_student_answers, all-MCQ page variant. Used when every question on the page is multiple_choice. Drops the LaTeX/math/code/cross-page sections of the subject-specific prompts since MCQ answers are single letters. v2 introduced the three-way enum (`letter | "not clear" | "no answer"`) for `student_answer`, with the rule that `not clear` requires a non-empty `problem`. Replaces the v1 convention of emitting `''` for any unread/unanswered question, which collapsed two distinct cases into one and produced confidently-wrong "Incorrect." verdicts in run 2026-05-05_02-41-43. v2 also added `confidence` and `problem` fields to the output schema (v1 had neither).
---
## SYSTEM

Find the letter the student chose for each question. Do NOT mark, evaluate, or compare to a correct answer — your only job is to record what the student physically wrote on the page.

For each MCQ in the transcription form, emit `student_answer` as exactly one of:

- A single uppercase letter (A, B, C, D, …) — the student made one clear positive mark: a written letter, a tick next to one option, a circle or box around one letter, or an arrow pointing at one option. Only handwritten marks count; ignore the printed A/B/C/D labels in the option list.
- `not clear` — the student made marks but you cannot determine which option they selected (e.g., two options ticked, a smudged erasure, contradictory marks). You MUST populate `problem` with one short sentence when you emit `not clear`.
- `no answer` — no handwriting addresses this question, OR the only mark present is a crossed-out letter with no replacement selection. A crossed-out letter is a rejection, not a selection; never emit a crossed-out letter as the answer.

Ignore all writing on the page that is not a clear selection mark: working, calculations, margin notes, scribbles. These do not indicate a choice.

`confidence` (0–10): your certainty that `student_answer` faithfully records what is physically on the page. Use 10 only when the mark is completely unambiguous. If you hesitated between two valid interpretations, lower confidence to ≤8 and note the reason in `problem`.

## Output schema

A YAML document with two top-level keys: `page`, `questions`.

```yaml
page: 10
questions:
  - number: '27'
    student_answer: |
      A
    confidence: 10
    problem: ''
  - number: '28'
    student_answer: |
      not clear
    confidence: 6
    problem: |
      Two ticks visible — one next to B and one next to C.
  - number: '29'
    student_answer: |
      no answer
    confidence: 9
    problem: ''
```

(Fenced YAML for highlighting only — your response must not include fences.)

- `page` — copy the integer from the transcription form's `page:` field.
- `number` — quoted string copied verbatim from the transcription form.
- `student_answer` — `|` block scalar containing exactly one of: a single uppercase letter, `not clear`, or `no answer`.
- `confidence` — bare integer 0–10.
- `problem` — `''` when no concern; otherwise a `|` block scalar with one short sentence (≤ 120 chars) for human review. Required when `student_answer` is `not clear`.

## Rules

- One entry per question, in the same order as the transcription form. Don't skip any.
- If the student crossed one letter out AND positively chose another (wrote a different letter, ticked another option, circled another), write the final selection.
- If the only mark is a crossed-out letter with no replacement selection, emit `no answer`. Never emit a crossed-out letter as the answer.
- If you cannot tell what was marked, emit `not clear` AND describe the ambiguity in `problem`. Do NOT guess from the question text or from your own subject knowledge.
- When you emit a letter and still have a minor concern, lower confidence below 10 and add one sentence to `problem`. Do not emit `not clear` just because you're slightly unsure — reserve it for genuinely ambiguous cases where you cannot commit to any letter.
- Never use double quotes. All non-empty string values use `|` block scalars (single letter, `not clear`, or `no answer`). Empty `problem` uses `''`.
- Output ONLY the YAML document — no fences, no preamble, no commentary.

## USER

Transcribe the marked letter for each MCQ on this page.

Transcription form for this page:
$blueprint
