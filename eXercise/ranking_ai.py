# -*- coding: utf-8 -*-
"""AI-call layer for the difficulty-ranking pipeline.

Three provider paths in order of preference:

1. Native Gemini PDF — uploads both PDFs as document parts via the new
   ``google-genai`` SDK; no page cap.
2. Native Qwen PDF — uploads via DashScope file-extract and references the
   files as ``fileid://`` system messages. Requires a Qwen model that
   accepts PDF input (see ``qwen_input.model_supports_pdf_input``).
3. Vision fallback — renders pages to base64 PNGs and sends them as
   ``image_url`` parts (capped at ``MAX_PAGES``); falls back to plain text
   extraction if the vision call fails.

The orchestrator in ``difficulty_ranking.py`` calls :func:`_rank_exercises_ai`
which dispatches to the right path based on the configured ``RANKING_MODEL``.
"""

from __future__ import annotations

import base64
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

try:
    import fitz  # PyMuPDF
    _FITZ_OK = True
except ImportError:
    _FITZ_OK = False

try:
    from .ai_client import (
        build_thinking_kwargs,
        collect_streamed_response,
        format_model_announcement,
        make_ai_client,
        print_streamed_response,
    )
    _AI_OK = True
except ImportError:
    _AI_OK = False

    def build_thinking_kwargs(provider: str, thinking_tokens: int | None) -> tuple[bool, dict]:  # type: ignore[misc]
        return False, {}

    def collect_streamed_response(stream: Any) -> str:  # type: ignore[misc]
        return ""

from .env_load import load_project_env

MAX_PAGES = 12  # cap to avoid token overflow


def _eprint(msg: str) -> None:
    """Print an error message to stderr — red in a TTY terminal, plain text otherwise."""
    if sys.stderr.isatty():
        print(f"\033[31m{msg}\033[0m", file=sys.stderr, flush=True)
    else:
        print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# PDF → images / text
# ---------------------------------------------------------------------------

def _pdf_to_b64_images(pdf_path: Path, dpi: int = 100) -> list[str]:
    """Render each page to a PNG base64 data-URL.  Capped at MAX_PAGES."""
    if not _FITZ_OK:
        return []
    images: list[str] = []
    try:
        doc = fitz.open(str(pdf_path))
        for page in doc:
            if len(images) >= MAX_PAGES:
                break
            pix = page.get_pixmap(dpi=dpi)
            png_bytes = pix.tobytes("png")
            b64 = base64.b64encode(png_bytes).decode()
            images.append(f"data:image/png;base64,{b64}")
        doc.close()
    except Exception as exc:
        _eprint(f"  Ranking: could not render {pdf_path.name} as images: {exc}")
    return images


def _extract_pdf_text(pdf_path: Path) -> str:
    """Extract plain text from a PDF as a fallback when vision is unavailable."""
    if not _FITZ_OK:
        return ""
    parts: list[str] = []
    try:
        doc = fitz.open(str(pdf_path))
        for page in doc:
            parts.append(page.get_text("text"))
        doc.close()
    except Exception as exc:
        _eprint(f"  Ranking: could not extract text from {pdf_path.name}: {exc}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# AI call
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert Cambridge IGCSE examiner.
You will be shown one or more exercise sheets and (optionally) their answer sheets.
Your task: rank every individual question part from most difficult to easiest.

CRITICAL: Only rank the questions that are EXPLICITLY VISIBLE in the provided documents.
Do NOT add, invent, or recall any questions from memory or prior knowledge of the exam paper.

Rules:
- If a question has sub-parts (a, b, c) or sub-sub-parts (a(i), a(ii)), rank each
  part individually. If a question has no sub-parts, rank the whole question.
- Always prefix the question number with "Q", e.g. "Q3b", "Q12a(i)", "Q7".
- Also prefix with the paper label before "Q":
  e.g. "w24/22 Q3b", "w24/12 Q5b(ii)".
- COMPLETE LIST REQUIRED: Your response MUST include EVERY question part visible
  in the documents, from most difficult down to easiest. Do NOT stop early.
  Do NOT omit questions because they seem easy or because the list is long.
- Start your response immediately with "1." — no prose, no preamble, no code fences.
  Do NOT say "Okay", "Here is", "Final check", or anything before the first list item.
- Do NOT wrap the list in code fences (``` or similar). Plain text only.
- Output ONLY a plain numbered list, one identifier per line.
  NO prose, NO headings, NO explanations, NO categories, NO tier names, NO plan steps.
- WRONG (never do this):
  1. Here's my plan:
  2. Hard tier: w24/22 Q10, w24/12 Q5b(ii)
  Mid Tier: ...
- CORRECT:
  1. w24/22 Q10
  2. w24/12 Q5b(ii)
  3. w24/22 Q3b
"""


def _rank_exercises_ai_gemini(
    exercise_pdf: Path,
    answer_pdf: Path | None,
    model: str,
    thinking_tokens: int | None,
    max_tokens: int | None,
    save_dir: Path | None = None,
    stream_thinking: bool = True,
) -> list[str]:
    """Rank exercises by uploading PDFs natively to the Gemini Files API.

    Uses ``google-genai`` (the new SDK) instead of the OpenAI-compat endpoint,
    so PDFs are sent as documents — no image rendering or page cap.
    """
    try:
        from google.genai import types as gai_types
    except ImportError:
        raise RuntimeError("google-genai not installed; run: pip install google-genai")

    from .ai_client import make_gemini_native_client  # noqa: PLC0415
    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError("GEMINI_API_KEY (or GOOGLE_API_KEY) not set")

    # Inline both PDFs via gemini_pdf_part — no upload pool, no polling.
    from .ai_client import build_gemini_thinking_config, gemini_pdf_part  # noqa: PLC0415

    print(f"  Reading PDF(s) for Gemini call…", flush=True)
    exercise_part = gemini_pdf_part(client, exercise_pdf, label="exercise")
    answers_part = (
        gemini_pdf_part(client, answer_pdf, label="answers")
        if (answer_pdf and answer_pdf.exists()) else None
    )

    thinking_cfg = build_gemini_thinking_config(thinking_tokens)

    gen_config = gai_types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        thinking_config=thinking_cfg,
        max_output_tokens=max_tokens or 32768,
    )

    # Build content parts — file parts interleaved with text labels
    if answers_part is not None:
        parts: list = [
            gai_types.Part.from_text(text="=== EXERCISE SHEET ==="),
            exercise_part,
            gai_types.Part.from_text(text="=== ANSWER SHEET ==="),
            answers_part,
        ]
    else:
        parts = [exercise_part]

    if save_dir:
        from .prompt_logger import save_prompt as _sp  # noqa: PLC0415
        _sp(
            save_dir / "ranking_prompt.json",
            model=model,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"[PDF: {exercise_pdf.name}]"
                       + (f" + [answers: {answer_pdf.name}]" if answer_pdf else "")}],
        )

    print("  Ranking (streaming):", flush=True)
    chunks: list[str] = []
    thinking_chunks: list[str] = []
    in_thinking = False
    try:
        for chunk in client.models.generate_content_stream(
            model=model,
            contents=parts,
            config=gen_config,
        ):
            for part in (chunk.candidates or [{}])[0].content.parts if (
                chunk.candidates and chunk.candidates[0].content and chunk.candidates[0].content.parts
            ) else []:
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
    finally:
        # Inline PDFs: nothing to clean up. >18 MB fallback uploads auto-expire
        # after 48 h via Gemini policy.
        pass

    raw = "".join(chunks)
    if save_dir:
        from .prompt_logger import save_response as _sr  # noqa: PLC0415
        _sr(
            save_dir / "ranking_prompt.json", raw,
            thinking="".join(thinking_chunks),
        )
        print(f"  Saved raw response: ranking_response.txt")
    return _parse_ranking(raw)


def _save_images(images: list[str], save_dir: Path, prefix: str) -> None:
    """Decode base64 data-URLs and write them as PNG files."""
    for i, data_url in enumerate(images, start=1):
        header, b64 = data_url.split(",", 1)
        ext = "jpg" if "jpeg" in header else "png"
        dest = save_dir / f"{prefix}_page_{i}.{ext}"
        dest.write_bytes(base64.b64decode(b64))
        print(f"    Saved image: {dest.name}")


def _rank_exercises_ai(
    exercise_pdf: Path,
    answer_pdf: Path | None,
    save_dir: Path | None = None,
    stream_thinking: bool = True,
) -> list[str]:
    """Call the LLM and return the ranked list of question identifiers."""
    if not _AI_OK:
        _eprint("  Ranking: ai_client not available.")
        return []

    load_project_env()

    result = make_ai_client(
        model_env="RANKING_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_model="qwen3.6-plus, high",
    )
    if result is None:
        _eprint("  Ranking: no API key set for ranking model; skipping.")
        return []

    client, model, provider, thinking_tokens, max_tokens = result
    eff_max = max_tokens if max_tokens is not None else 32768
    print(f"  {format_model_announcement(model, thinking_tokens, eff_max)}")

    # Native Gemini path: upload PDFs directly — no image rendering needed
    if provider == "gemini":
        try:
            ranking = _rank_exercises_ai_gemini(
                exercise_pdf, answer_pdf, model, thinking_tokens, max_tokens,
                save_dir=save_dir, stream_thinking=stream_thinking,
            )
            print(f"  Ranked {len(ranking)} question part(s).")
            return ranking
        except Exception as exc:
            _eprint(f"  Ranking: native Gemini path failed ({type(exc).__name__}: {exc}); falling back to image path.")

    # Native Qwen path: upload PDFs via DashScope file-extract and reference
    # them as fileid:// system messages. qwen-doc-turbo allows only one PDF per
    # call, so when both exercise and answer PDFs are present we require
    # qwen-long (multi-file). On any failure, fall through to the vision path.
    from .qwen_input import (  # noqa: PLC0415
        model_supports_multi_pdf_input,
        model_supports_pdf_input,
        qwen_pdf_system_message,
        upload_pdf_for_extract,
    )
    _has_answers = bool(answer_pdf and answer_pdf.exists())
    _use_qwen_pdf = (
        provider == "qwen" and model_supports_pdf_input(model)
    )
    if _use_qwen_pdf and _has_answers and not model_supports_multi_pdf_input(model):
        _eprint(
            f"  Ranking: model {model!r} accepts only 1 PDF per call but "
            f"exercise + answer PDFs were both provided; falling back to "
            f"vision. Set RANKING_MODEL=qwen-long for native PDF here."
        )
        _use_qwen_pdf = False

    def _build_vision_messages() -> list[dict]:
        has_answers = bool(answer_pdf and answer_pdf.exists())
        print(
            f"  Rendering PDFs as images (exercise"
            + (f" + answers" if has_answers else "")
            + ", in parallel)…",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_ex = pool.submit(_pdf_to_b64_images, exercise_pdf)
            fut_ans = pool.submit(_pdf_to_b64_images, answer_pdf) if has_answers else None
            ex_images = fut_ex.result()
            ans_images = fut_ans.result() if fut_ans else []

        print(f"    Exercise sheet: {len(ex_images)} page(s)")
        if save_dir and ex_images:
            _save_images(ex_images, save_dir, "ranking_ex")
        content: list[dict] = []
        content.append({"type": "text", "text": "=== EXERCISE SHEET ==="})
        for img in ex_images:
            content.append({"type": "image_url", "image_url": {"url": img}})
        if ans_images:
            print(f"    Answer sheet: {len(ans_images)} page(s)")
            if save_dir:
                _save_images(ans_images, save_dir, "ranking_ans")
            content.append({"type": "text", "text": "=== ANSWER SHEET ==="})
            for img in ans_images:
                content.append({"type": "image_url", "image_url": {"url": img}})
        else:
            print("    Answer sheet: not included")
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ]

    def _build_text_messages() -> list[dict]:
        print("  Extracting text from PDFs (vision unavailable)…", flush=True)
        ex_text = _extract_pdf_text(exercise_pdf)
        parts = ["=== EXERCISE SHEET ===\n" + ex_text]
        if answer_pdf and answer_pdf.exists():
            ans_text = _extract_pdf_text(answer_pdf)
            if ans_text.strip():
                parts.append("=== ANSWER SHEET ===\n" + ans_text)
        return [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(parts)},
        ]

    ranking_thinking: list[str] = []

    _vision_max_tokens = max_tokens or 8192

    def _call(messages: list[dict]) -> str:
        use_stream, thinking_kw = build_thinking_kwargs(provider, thinking_tokens)
        print("  Waiting for AI response…", flush=True)
        if use_stream:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                max_tokens=_vision_max_tokens,
                **thinking_kw,
            )
            return print_streamed_response(
                stream, stream_thinking=stream_thinking, print_content=False, thinking_out=ranking_thinking,
            )
        completion = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=_vision_max_tokens,
            **thinking_kw,
        )
        return (completion.choices[0].message.content or "").strip()

    raw: str | None = None

    # First attempt: Qwen native PDF (when model supports it).
    if _use_qwen_pdf:
        try:
            print("  Uploading PDF(s) for Qwen native PDF call…", flush=True)
            _ex_id = upload_pdf_for_extract(client, exercise_pdf)
            _ans_id = (
                upload_pdf_for_extract(client, answer_pdf) if _has_answers else None
            )
            _qwen_user_lines = ["Materials attached (referenced via fileid:// above):"]
            _qwen_user_lines.append("- EXERCISE SHEET: first fileid")
            if _ans_id is not None:
                _qwen_user_lines.append("- ANSWER SHEET: second fileid")
            _qwen_user_lines.append("")
            _qwen_user_lines.append(
                "Rank every question part in the exercise sheet from most "
                "difficult to easiest, in the format described."
            )
            _qwen_user_text = "\n".join(_qwen_user_lines)
            _qwen_msgs: list[dict] = [
                {"role": "system", "content": _SYSTEM_PROMPT},
                qwen_pdf_system_message(_ex_id),
            ]
            if _ans_id is not None:
                _qwen_msgs.append(qwen_pdf_system_message(_ans_id))
            _qwen_msgs.append({"role": "user", "content": _qwen_user_text})
            if save_dir:
                from .prompt_logger import save_prompt as _sp  # noqa: PLC0415
                _sp(
                    save_dir / "ranking_prompt.json",
                    model=model,
                    system=_SYSTEM_PROMPT,
                    messages=[{
                        "role": "user",
                        "content": (
                            f"[PDF (qwen fileid): {exercise_pdf.name}"
                            + (
                                f" + answers: {answer_pdf.name}"
                                if _has_answers and answer_pdf is not None
                                else ""
                            )
                            + f"]\n\n{_qwen_user_text}"
                        ),
                    }],
                )
            raw = _call(_qwen_msgs)
        except Exception as exc:
            _eprint(
                f"  Ranking: Qwen native-PDF call failed "
                f"({type(exc).__name__}: {exc}); falling back to vision."
            )
            raw = None

    # Vision fallback (also the default path when Qwen-PDF isn't available).
    if raw is None:
        try:
            _vision_msgs = _build_vision_messages()
            if save_dir:
                from .prompt_logger import save_prompt as _sp  # noqa: PLC0415
                _sp(
                    save_dir / "ranking_prompt.json",
                    model=model,
                    system=_SYSTEM_PROMPT,
                    messages=_vision_msgs[1:],  # skip system message — it's in `system` param
                )
            raw = _call(_vision_msgs)
        except Exception as exc:
            _eprint(f"  Ranking: vision call failed ({type(exc).__name__}: {exc}); retrying with text.")
            try:
                raw = _call(_build_text_messages())
            except Exception as exc2:
                _eprint(f"  Ranking: text fallback also failed ({type(exc2).__name__}: {exc2})")
                return []

    if save_dir:
        from .prompt_logger import save_response as _sr  # noqa: PLC0415
        _sr(
            save_dir / "ranking_prompt.json", raw,
            thinking="".join(ranking_thinking),
        )
        print(f"  Saved raw response: ranking_response.txt", flush=True)
    ranking = _parse_ranking(raw)
    if not ranking and raw.strip():
        _eprint("  Ranking: response was non-empty but produced no lines after parsing.")
    print(f"  Ranked {len(ranking)} question part(s).")
    return ranking


# Matches any line that contains a question identifier: Q followed by at least one digit.
# This admits "s23/21 Q4", "Q3b", "w24/22 Q12a(ii)" while rejecting prose, code fences, headings.
_QUESTION_LINE_RE = re.compile(r'Q\d', re.IGNORECASE)


def _parse_ranking(response: str) -> list[str]:
    """Strip numbered-list prefixes; keep only lines containing a question identifier (Q + digit)."""
    ranking: list[str] = []
    seen: set[str] = set()
    for line in response.strip().splitlines():
        cleaned = re.sub(r"^\s*\d+[\.\)]\s*", "", line).strip()
        if cleaned and cleaned not in seen and _QUESTION_LINE_RE.search(cleaned):
            seen.add(cleaned)
            ranking.append(cleaned)
    return ranking
