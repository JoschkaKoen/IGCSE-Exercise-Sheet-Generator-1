# -*- coding: utf-8 -*-
"""AI prompting for MCQ explanation generation.

Builds system and user prompts, calls the LLM, and parses the
delimiter-based response into structured bullet-point explanations.
"""

from __future__ import annotations

import re
import time
from typing import Any

# ---------------------------------------------------------------------------
# Subject-specific AI prompt fragments
# ---------------------------------------------------------------------------

_SUBJECT_HINTS: dict[str, str] = {
    "physics": (
        "Use LaTeX notation for ALL mathematical expressions and physical quantities: "
        "inline math with $...$ (e.g. $F = ma$, $E_k = \\frac{1}{2}mv^2$, $R = \\frac{V}{I}$). "
        "For display equations use $$...$$. "
        "Do NOT use any macros from the `physics` LaTeX package (\\dv, \\pdv, \\qty, etc.). "
        "Write units in roman style inside math: $\\mathrm{m\\,s^{-2}}$."
    ),
    "mathematics": (
        "Use LaTeX notation for ALL mathematical expressions: $...$ for inline, $$...$$ for display. "
        "Use standard amsmath notation only — no custom packages."
    ),
    "computer_science": (
        "Where relevant, show short pseudocode using a verbatim block (\\verb|...|) or \\texttt{...}. "
        "Use LaTeX $...$ only for mathematical sub-expressions. "
        "Explain logic and algorithms in plain English, not code."
    ),
    "biology": (
        "Use precise biological terminology. "
        "Use LaTeX $...$ only for mathematical sub-expressions (e.g. magnification calculations). "
        "Explain processes and mechanisms in clear, concise biological terms — no unnecessary jargon."
    ),
    "chemistry": (
        "Use correct chemical notation: write formulae in \\ce{...} using the mhchem package "
        "(e.g. \\ce{H2O}, \\ce{CO2}, \\ce{NaCl}). "
        "Use LaTeX $...$ for numerical expressions and equations (e.g. $M_r$, $\\Delta H$). "
        "Name compounds and state symbols where relevant."
    ),
}

_DEFAULT_SUBJECT_HINT = (
    "Use LaTeX $...$ for any inline mathematical expressions and $$...$$ for display equations."
)

# ---------------------------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATE = """\
You are an expert Cambridge IGCSE {subject_title} tutor.

You will receive a list of multiple-choice questions with their correct answers.
For each question return exactly 3 concise bullet-point explanations.

Rules:
{subject_hint}
{gemini_brevity}
- Write in clear, plain English suitable for non-native English speakers (IGCSE, age 14–16). Use simple, everyday vocabulary — avoid difficult or academic words  when a simpler phrase works. Avoid academic and difficult words for non native grade 10 students.
- Each bullet is short and concise (and clear).
- The goal is to explain to the students the correct answer so they can understand the correct answer as well and as easy as possible.
- The explanation should be easy to read and understand.
- Explain WHY the correct answer is right; briefly dismiss the most tempting distractor.
- Some questions include an image of diagrams or figures extracted from the exam paper. Use the image to understand visual content (circuit diagrams, graphs, answer-option diagrams labelled A–D, etc.) that the plain text alone cannot convey.
- Do NOT restate the question text. Do NOT say "the answer is X" — explain the reasoning.
- Output using this EXACT plain-text delimiter format (NOT JSON, NOT markdown):

===Q1===
First bullet point
---
Second bullet point
---
Third bullet point
===Q2===
First bullet point
---
Second bullet point
---
Third bullet point

- Use ===Q<number>=== to start each question (e.g. ===Q38===).
- Separate the 3 bullets with --- on its own line.
- Every question number you receive must appear in the output.
- Do NOT wrap in JSON, code fences, or any other format.\
"""

# Extra constraints for Gemini models (they tend to over-explain).
_GEMINI_BREVITY_RULES = """\
- SIMPLE ENGLISH: Everyday words and short clauses only. Avoid fancy or academic vocabulary where a plain word works (say "pulls" not "exerts an attractive force upon", "same" not "equivalent").
- SHORT BULLETS ONLY: Each bullet is at most ONE short sentence, ideally under ~18 words. No warm-up phrases ("Firstly", "It is important to note", "This means that").
- Students must grasp each point in a quick skim — telegraphic style is good: name the idea, link it to the correct option, stop.
- Prefer one tight sentence per bullet over two looser ones (even if "1–2 sentences" appears elsewhere in these rules).
"""

_SUBJECT_TITLES: dict[str, str] = {
    "physics": "Physics",
    "mathematics": "Mathematics",
    "computer_science": "Computer Science",
    "biology": "Biology",
    "chemistry": "Chemistry",
}


def _build_system_prompt(exam_key: str | None, provider: str = "") -> str:
    key = exam_key or ""
    title = _SUBJECT_TITLES.get(key, "Science")
    hint = _SUBJECT_HINTS.get(key, _DEFAULT_SUBJECT_HINT)
    gemini_brevity = _GEMINI_BREVITY_RULES if provider == "gemini" else ""
    return _SYSTEM_TEMPLATE.format(
        subject_title=title, subject_hint=hint, gemini_brevity=gemini_brevity
    )


def _build_user_message(
    q_texts: dict[int, str],
    answers: dict[int, str],
    questions: list[int],
) -> str:
    parts = ["Questions and correct answers:\n"]
    for q in questions:
        if q not in answers:
            continue
        ans = answers[q]
        text = q_texts.get(q, "").strip() or "(question text unavailable)"
        parts.append(f"Q{q} (Answer: {ans})\n{text}")
    return "\n\n".join(parts)


def _build_user_content(
    q_texts: dict[int, str],
    answers: dict[int, str],
    questions: list[int],
    q_images: dict[int, str],
) -> str | list[dict]:
    """Build user message content, using vision format when images are present.

    Returns a plain string when *q_images* is empty (backward-compatible) or a
    list of ``{"type": "text"/"image_url", ...}`` content parts for the
    OpenAI-compatible vision API.
    """
    if not q_images:
        return _build_user_message(q_texts, answers, questions)

    # Build multimodal content: interleave text and images so the model sees
    # each image right after the question it belongs to.
    parts: list[dict] = []
    text_buf: list[str] = ["Questions and correct answers:\n"]

    for q in questions:
        if q not in answers:
            continue
        ans = answers[q]
        text = q_texts.get(q, "").strip() or "(question text unavailable)"
        text_buf.append(f"Q{q} (Answer: {ans})\n{text}")

        if q in q_images:
            text_buf.append(f"(See attached image for Q{q} below.)")
            # Flush accumulated text, then insert image.
            parts.append({"type": "text", "text": "\n\n".join(text_buf)})
            text_buf = []
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{q_images[q]}"},
            })

    # Flush any remaining text after the last question.
    if text_buf:
        parts.append({"type": "text", "text": "\n\n".join(text_buf)})

    return parts


def _parse_explanations(raw: str, questions: list[int]) -> dict[int, list[str]] | None:
    """Parse delimiter-based AI response; return dict or None on total failure.

    Expected format::

        ===Q38===
        Bullet 1
        ---
        Bullet 2
        ---
        Bullet 3
        ===Q39===
        ...

    Accepts partial responses: questions missing from the response or with fewer
    than 3 bullets are padded with empty-string placeholders rather than dropped,
    so the template can still render a "(Explanation not available.)" for them.
    """
    # Split on ===Q<num>=== headers
    parts = re.split(r'===Q(\d+)===', raw)
    # parts[0] is preamble (before first header), then alternating: qnum_str, content
    if len(parts) < 3:
        print(f"    No ===Q<num>=== delimiters found in response of length {len(raw)}.")
        return None

    parsed: dict[int, list[str]] = {}
    for i in range(1, len(parts), 2):
        qnum = int(parts[i])
        if i + 1 < len(parts):
            body = parts[i + 1].strip()
            bullets = [b.strip() for b in body.split('\n---\n')]
            # Filter empty bullets, keep up to 3
            bullets = [b for b in bullets if b.strip()][:3]
        else:
            bullets = []
        parsed[qnum] = bullets

    # Build result for requested questions, padding to 3 bullets
    result: dict[int, list[str]] = {}
    for q in questions:
        bullets = parsed.get(q, [])
        while len(bullets) < 3:
            bullets.append("")
        result[q] = bullets
    return result if result else None


def generate_mcq_explanations(
    client: Any,
    model: str,
    q_texts: dict[int, str],
    answers: dict[int, str],
    questions: list[int],
    exam_key: str | None,
    q_images: dict[int, str] | None = None,
    provider: str = "",
    effort: str | None = None,
) -> dict[int, list[str]]:
    """Call the AI once for all questions; return ``{qnum: [bullet, bullet, bullet]}``.

    When *q_images* is provided (``{qnum: base64_png}``), the user message is
    sent in multimodal (vision) format so the model can see diagrams and figures.

    Returns an empty dict on any error so the caller can fall back gracefully.
    """
    from .ai_client import build_thinking_kwargs, print_streamed_response  # noqa: PLC0415

    questions_with_answers = [q for q in questions if q in answers]
    if not questions_with_answers:
        return {}

    system = _build_system_prompt(exam_key, provider)
    user_content: str | list[dict] = _build_user_content(
        q_texts, answers, questions_with_answers, q_images or {},
    )

    use_stream, thinking_kw = build_thinking_kwargs(provider, effort)

    def _call(**kwargs: Any) -> tuple[str, str | None]:
        """Return (content, finish_reason)."""
        msgs = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]
        if use_stream:
            stream = client.chat.completions.create(
                model=model,
                max_tokens=16384,
                messages=msgs,
                stream=True,
                **thinking_kw,
                **kwargs,
            )
            text = print_streamed_response(stream, print_thinking=True, print_content=True)
            return text, "stop" if text else "length"
        completion = client.chat.completions.create(
            model=model,
            max_tokens=16384,
            messages=msgs,
            **thinking_kw,
            **kwargs,
        )
        choice = completion.choices[0]
        text = (choice.message.content or "").strip()
        return text, getattr(choice, "finish_reason", None)

    max_attempts = 3

    for attempt in range(max_attempts):
        try:
            _t0 = time.monotonic()
            raw, finish = _call()
            print(f"  MCQ explanations: {time.monotonic() - _t0:.1f}s")
        except Exception as exc:
            print(f"  MCQ explanations: API error on attempt {attempt + 1}: {exc}")
            if attempt == max_attempts - 1:
                return {}
            continue

        if finish and finish != "stop":
            print(f"  MCQ explanations: response truncated (finish_reason={finish}, {len(raw)} chars)")

        result = _parse_explanations(raw, questions_with_answers)
        if result:
            return result

        # Show enough of the raw response to diagnose the parse failure.
        preview = raw[:500] if raw else "(empty)"
        tail = raw[-200:] if len(raw) > 500 else ""
        print(f"  MCQ explanations: parse failed on attempt {attempt + 1} ({len(raw)} chars, finish={finish}). Raw start: {preview}")
        if tail:
            print(f"  Raw tail: …{tail}")
        if attempt < max_attempts - 1:
            print("  Retrying…")
            nudge = (
                '\n\nYou MUST use the ===Q<number>=== delimiter format. '
                'Do NOT use JSON. Separate bullets with --- on its own line.'
            )
            if isinstance(user_content, str):
                user_content = user_content + nudge
            else:
                user_content = user_content + [{"type": "text", "text": nudge}]

    return {}
