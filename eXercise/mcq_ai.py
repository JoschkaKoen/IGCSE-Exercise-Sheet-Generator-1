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


def _save_mcq_prompt(
    save_dir: Any,
    system: str,
    user_content: str | list[dict],
    exam_key: str | None,
    q_texts: dict[int, str] | None = None,
) -> None:
    """Write the full prompt and extracted question texts to the output dir."""
    from pathlib import Path  # noqa: PLC0415
    Path(save_dir).mkdir(parents=True, exist_ok=True)

    # Full prompt
    lines: list[str] = ["=== SYSTEM PROMPT ===", system, "", "=== USER CONTENT ==="]
    if isinstance(user_content, str):
        lines.append(user_content)
    else:
        for part in user_content:
            if part.get("type") == "text":
                lines.append(part["text"])
            elif part.get("type") == "image_url":
                url: str = part.get("image_url", {}).get("url", "")
                lines.append(f"[IMAGE: base64 PNG, {len(url)} chars]")
    (Path(save_dir) / "mcq_expl_prompt.txt").write_text("\n".join(lines), encoding="utf-8")
    print(f"  Saved MCQ prompt: mcq_expl_prompt.txt")

    # JSON version (stripped of images) for machine-readable audit
    try:
        from xscore.shared.prompt_logger import save_prompt as _sp  # noqa: PLC0415
        _sp(
            Path(save_dir) / "mcq_expl_prompt.json",
            model="",
            system=system,
            messages=[{"role": "user", "content": user_content if isinstance(user_content, str) else
                       " ".join(p.get("text", "") for p in user_content if isinstance(p, dict) and p.get("type") == "text")}],
        )
    except Exception:
        pass

    # Raw extracted question texts
    if q_texts:
        text_lines: list[str] = []
        for qnum in sorted(q_texts):
            text_lines.append(f"Q{qnum}:\n{q_texts[qnum]}")
        (Path(save_dir) / "mcq_expl_texts.txt").write_text("\n\n".join(text_lines), encoding="utf-8")
        print(f"  Saved MCQ extracted texts: mcq_expl_texts.txt")


def generate_mcq_explanations_gemini_pdf(
    q_pdf_bytes: bytes,
    answers: dict[int, str],
    questions: list[int],
    exam_key: str | None,
    model: str,
    effort: str | None = None,
    save_dir: Any | None = None,
    stream_thinking: bool = True,
) -> dict[int, list[str]]:
    """Generate MCQ explanations by uploading a questions PDF to the Gemini Files API.

    Uses the native ``google-genai`` SDK (same as difficulty_ranking) rather than
    the OpenAI-compat endpoint, so the full PDF is sent as a document — no text
    parsing or image rasterization required.

    Returns ``{qnum: [bullet, bullet, bullet]}`` or ``{}`` on any error.
    """
    import os as _os
    import tempfile
    import time as _time
    from pathlib import Path as _Path

    try:
        from google import genai as gai
        from google.genai import types as gai_types
    except ImportError:
        print("  MCQ explanations (PDF): google-genai not installed; falling back.")
        return {}

    api_key = (
        _os.environ.get("GEMINI_API_KEY", "") or _os.environ.get("GOOGLE_API_KEY", "")
    ).strip()
    if not api_key:
        print("  MCQ explanations (PDF): GEMINI_API_KEY not set; falling back.")
        return {}

    client = gai.Client(api_key=api_key)

    # Write PDF bytes to a temp file for upload, then delete it immediately.
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp_path = f.name
        f.write(q_pdf_bytes)

    file_obj = None
    try:
        print("  Uploading MCQ questions PDF to Gemini Files API…", flush=True)
        file_obj = client.files.upload(file=_Path(tmp_path))
    finally:
        try:
            _os.unlink(tmp_path)
        except OSError:
            pass

    try:
        # Poll until the file is ready.
        while getattr(file_obj.state, "name", str(file_obj.state)) == "PROCESSING":
            print("    Waiting for PDF to be processed…", flush=True)
            _time.sleep(2)
            file_obj = client.files.get(name=file_obj.name)
        state = getattr(file_obj.state, "name", str(file_obj.state))
        if state == "FAILED":
            print("  MCQ explanations (PDF): Gemini file processing failed; falling back.")
            return {}
        print(f"    PDF ready ({file_obj.name}).")

        system_prompt = _build_system_prompt(exam_key, provider="gemini")

        def _build_pdf_user_text(nudge: str = "") -> str:
            lines = [
                "The attached PDF contains the MCQ questions for this paper.",
                "Generate explanations for each question listed below.\n",
            ]
            for q in questions:
                if q in answers:
                    lines.append(f"Q{q} (Answer: {answers[q]})")
            if nudge:
                lines.append(nudge)
            return "\n".join(lines)

        user_text = _build_pdf_user_text()

        if save_dir is not None:
            from pathlib import Path as _P  # noqa: PLC0415
            _P(save_dir).mkdir(parents=True, exist_ok=True)
            debug_lines = [
                "=== SYSTEM PROMPT ===", system_prompt, "",
                "=== USER TEXT ===", user_text, "",
                f"=== PDF: {len(q_pdf_bytes):,} bytes uploaded as {file_obj.name} ===",
            ]
            (_P(save_dir) / "mcq_expl_prompt_pdf.txt").write_text(
                "\n".join(debug_lines), encoding="utf-8"
            )
            print("  Saved MCQ prompt (PDF path): mcq_expl_prompt_pdf.txt")
            try:
                from xscore.shared.prompt_logger import save_prompt as _sp  # noqa: PLC0415
                _sp(
                    _P(save_dir) / "mcq_expl_prompt_pdf.json",
                    model=model,
                    system=system_prompt,
                    messages=[{"role": "user", "content": user_text}],
                )
            except Exception:
                pass

        # Thinking config — mirror difficulty_ranking.py exactly.
        if effort == "off":
            thinking_cfg = gai_types.ThinkingConfig(thinking_budget=0, include_thoughts=False)
        elif effort == "low":
            thinking_cfg = gai_types.ThinkingConfig(thinking_budget=1024, include_thoughts=True)
        elif effort == "high":
            thinking_cfg = gai_types.ThinkingConfig(thinking_budget=8192, include_thoughts=True)
        else:
            thinking_cfg = gai_types.ThinkingConfig(include_thoughts=True)

        gen_config = gai_types.GenerateContentConfig(
            system_instruction=system_prompt,
            thinking_config=thinking_cfg,
            max_output_tokens=16384,
        )

        _NUDGE = (
            "\n\nYou MUST use the ===Q<number>=== delimiter format. "
            "Do NOT use JSON. Separate bullets with --- on its own line."
        )

        max_attempts = 3
        for attempt in range(max_attempts):
            cur_user_text = _build_pdf_user_text(_NUDGE if attempt > 0 else "")
            contents = [
                gai_types.Part.from_uri(file_uri=file_obj.uri, mime_type="application/pdf"),
                gai_types.Part.from_text(text=cur_user_text),
            ]

            try:
                chunks: list[str] = []
                thinking_chunks: list[str] = []
                in_thinking = False
                for chunk in client.models.generate_content_stream(
                    model=model,
                    contents=contents,
                    config=gen_config,
                ):
                    for part in (
                        chunk.candidates[0].content.parts
                        if (
                            chunk.candidates
                            and chunk.candidates[0].content
                            and chunk.candidates[0].content.parts
                        )
                        else []
                    ):
                        is_thought = getattr(part, "thought", False)
                        text = part.text or ""
                        if not text:
                            continue
                        if is_thought:
                            thinking_chunks.append(text)
                            if not in_thinking:
                                print("  [thinking]", flush=True)
                                in_thinking = True
                            if stream_thinking:
                                print(text, end="", flush=True)
                        else:
                            if in_thinking:
                                print("\n  [/thinking]", flush=True)
                                in_thinking = False
                            chunks.append(text)
                if in_thinking:
                    print()

            except Exception as exc:
                print(f"  MCQ explanations (PDF): API error on attempt {attempt + 1}: {exc}")
                if attempt == max_attempts - 1:
                    return {}
                continue

            raw = "".join(chunks)
            if save_dir is not None:
                from pathlib import Path as _P  # noqa: PLC0415
                (_P(save_dir) / "mcq_expl_response.txt").write_text(raw, encoding="utf-8")
                print("  Saved MCQ response: mcq_expl_response.txt")
                if thinking_chunks:
                    (_P(save_dir) / "mcq_expl_thinking.txt").write_text(
                        "".join(thinking_chunks), encoding="utf-8"
                    )
            result = _parse_explanations(raw, questions)
            if result:
                return result

            preview = raw[:500] if raw else "(empty)"
            print(
                f"  MCQ explanations (PDF): parse failed on attempt {attempt + 1} "
                f"({len(raw)} chars). Raw start: {preview}"
            )
            if attempt < max_attempts - 1:
                print("  Retrying…")

        return {}

    finally:
        if file_obj is not None:
            try:
                client.files.delete(name=file_obj.name)
            except Exception:
                pass


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
    save_dir: Any | None = None,  # Path | None — avoid import at module level
    q_pdf_bytes: bytes | None = None,
    stream_thinking: bool = True,
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

    # Gemini PDF path: upload the questions PDF natively instead of using text + images.
    if q_pdf_bytes is not None and provider == "gemini":
        return generate_mcq_explanations_gemini_pdf(
            q_pdf_bytes=q_pdf_bytes,
            answers=answers,
            questions=questions_with_answers,
            exam_key=exam_key,
            model=model,
            effort=effort,
            save_dir=save_dir,
            stream_thinking=stream_thinking,
        )

    system = _build_system_prompt(exam_key, provider)
    user_content: str | list[dict] = _build_user_content(
        q_texts, answers, questions_with_answers, q_images or {},
    )

    if save_dir is not None:
        _save_mcq_prompt(save_dir, system, user_content, exam_key, q_texts=q_texts)

    use_stream, thinking_kw = build_thinking_kwargs(provider, effort)
    thinking_parts: list[str] = []

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
            finish_out: list[str] = []
            text = print_streamed_response(
                stream, stream_thinking=stream_thinking, print_content=False,
                thinking_out=thinking_parts, finish_reason_out=finish_out,
            )
            return text, (finish_out[-1] if finish_out else None)
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
            raw, finish = _call()
            if save_dir is not None:
                from pathlib import Path as _P  # noqa: PLC0415
                (_P(save_dir) / "mcq_expl_response.txt").write_text(raw, encoding="utf-8")
                print("  Saved MCQ response: mcq_expl_response.txt")
                if thinking_parts:
                    (_P(save_dir) / "mcq_expl_thinking.txt").write_text(
                        "".join(thinking_parts), encoding="utf-8"
                    )
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
