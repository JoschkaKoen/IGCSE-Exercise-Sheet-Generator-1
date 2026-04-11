# -*- coding: utf-8 -*-
"""Generate a difficulty-ranking PDF for an exercise sheet.

Call :func:`generate_difficulty_ranking` at the end of a pipeline run.

The function:
1. Sends both the exercise PDF and (optionally) the answer PDF as images
   to an LLM (default: RANKING_MODEL env var, falls back to AI_DEFAULT_MODEL).
2. Parses the returned numbered list of question identifiers.
3. Typesets the ranked list with pdflatex.
4. Saves ``{name}_ranking.pdf`` next to the exercise sheet.

Set ``RANKING_SKIP=true`` in ``.env`` / environment to disable silently.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import tempfile
import time
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
        make_ai_client,
    )
    _AI_OK = True
except ImportError:
    _AI_OK = False

    def build_thinking_kwargs(provider: str, effort: str | None) -> tuple[bool, dict]:  # type: ignore[misc]
        return False, {}

    def collect_streamed_response(stream: Any) -> str:  # type: ignore[misc]
        return ""

from .env_load import load_project_env

MAX_PAGES = 12  # cap to avoid token overflow

# ---------------------------------------------------------------------------
# LaTeX helpers
# ---------------------------------------------------------------------------

_LATEX_SPECIAL = str.maketrans({
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "#": r"\#",
    "%": r"\%",
    "&": r"\&",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
})


def _latex_escape(text: str) -> str:
    return text.translate(_LATEX_SPECIAL)


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
        print(f"  Ranking: could not render {pdf_path.name} as images: {exc}")
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
        print(f"  Ranking: could not extract text from {pdf_path.name}: {exc}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# AI call
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert Cambridge IGCSE examiner.
You will be shown an exercise sheet and (optionally) its answer sheet.
Your task: rank every individual question part from most difficult to easiest.

CRITICAL: Only rank the questions that are EXPLICITLY VISIBLE in the provided documents.
Do NOT add, invent, or recall any questions from memory or prior knowledge of the exam paper.
If the sheet contains 3 questions, your output must contain exactly 3 entries.

Rules:
- If a question has sub-parts (a, b, c) or sub-sub-parts (a(i), a(ii)), rank each
  part individually. If a question has no sub-parts, rank the whole question.
- Always prefix the question number with "Q", e.g. "Q3b", "Q12a(i)", "Q7".
- Also prefix with the paper label before "Q":
  e.g. "w24/22 Q3b", "w24/12 Q5b(ii)".
- Output ONLY a plain numbered list, one identifier per line. No prose, no headings.
- Example output:
  1. w24/22 Q10
  2. w24/12 Q5b(ii)
  3. w24/22 Q3b
"""


def _rank_exercises_ai_gemini(
    exercise_pdf: Path,
    answer_pdf: Path | None,
    model: str,
    effort: str | None,
) -> list[str]:
    """Rank exercises by uploading PDFs natively to the Gemini Files API.

    Uses ``google-genai`` (the new SDK) instead of the OpenAI-compat endpoint,
    so PDFs are sent as documents — no image rendering or page cap.
    """
    try:
        from google import genai as gai
        from google.genai import types as gai_types
    except ImportError:
        raise RuntimeError("google-genai not installed; run: pip install google-genai")

    api_key = os.environ.get("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GOOGLE_API_KEY not set")

    client = gai.Client(api_key=api_key)

    # Upload both PDFs in parallel
    pdfs_to_upload: list[tuple[str, Path]] = [("exercise", exercise_pdf)]
    if answer_pdf and answer_pdf.exists():
        pdfs_to_upload.append(("answers", answer_pdf))

    print(f"  Uploading {len(pdfs_to_upload)} PDF(s) to Gemini Files API…", flush=True)

    def _upload(item: tuple[str, Path]):
        label, path = item
        return label, client.files.upload(file=path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        uploaded: list[tuple[str, object]] = list(pool.map(_upload, pdfs_to_upload))

    # Poll each file until ACTIVE
    ready: dict[str, object] = {}
    for label, f in uploaded:
        while getattr(f.state, "name", str(f.state)) == "PROCESSING":
            print(f"    Waiting for {label} PDF…", flush=True)
            time.sleep(3)
            f = client.files.get(name=f.name)
        state = getattr(f.state, "name", str(f.state))
        if state == "FAILED":
            raise RuntimeError(f"Gemini file processing failed ({label}): {f.name}")
        print(f"    {label.capitalize()} PDF ready ({f.name}).")
        ready[label] = f

    # Build thinking config — include_thoughts=True makes thought summaries visible
    if effort == "off":
        thinking_cfg = gai_types.ThinkingConfig(thinking_budget=0, include_thoughts=False)
    elif effort == "low":
        thinking_cfg = gai_types.ThinkingConfig(thinking_budget=1024, include_thoughts=True)
    elif effort == "high":
        thinking_cfg = gai_types.ThinkingConfig(thinking_budget=8192, include_thoughts=True)
    else:
        # Default: enable thoughts so they are visible in console
        thinking_cfg = gai_types.ThinkingConfig(include_thoughts=True)

    gen_config = gai_types.GenerateContentConfig(
        system_instruction=_SYSTEM_PROMPT,
        thinking_config=thinking_cfg,
    )

    # Build content parts — file parts interleaved with text labels
    parts: list = [
        gai_types.Part.from_uri(file_uri=ready["exercise"].uri, mime_type="application/pdf"),
    ]
    if "answers" in ready:
        parts = (
            [gai_types.Part.from_text(text="=== EXERCISE SHEET ===")]
            + parts
            + [
                gai_types.Part.from_text(text="=== ANSWER SHEET ==="),
                gai_types.Part.from_uri(file_uri=ready["answers"].uri, mime_type="application/pdf"),
            ]
        )

    print("  Ranking (streaming):", flush=True)
    chunks: list[str] = []
    in_thinking = False
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
                if not in_thinking:
                    print("  [thinking]", flush=True)
                    in_thinking = True
                print(text, end="", flush=True)
            else:
                if in_thinking:
                    print("\n  [/thinking]", flush=True)
                    in_thinking = False
                print(text, end="", flush=True)
                chunks.append(text)
    if in_thinking:
        print()
    print()  # newline after streamed output

    # Delete uploaded files (auto-expire after 48h anyway)
    for label, f in ready.items():
        try:
            client.files.delete(name=f.name)
        except Exception:
            pass

    return _parse_ranking("".join(chunks))


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
) -> list[str]:
    """Call the LLM and return the ranked list of question identifiers."""
    if not _AI_OK:
        print("  Ranking: ai_client not available.")
        return []

    load_project_env()

    result = make_ai_client(
        model_env="RANKING_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_model="qwen3.6-plus, high",
    )
    if result is None:
        print("  Ranking: no API key set for ranking model; skipping.")
        return []

    client, model, provider, effort = result
    effort_label = f", thinking={effort}" if effort else ""
    print(f"  Model: {model}{effort_label}")

    # Native Gemini path: upload PDFs directly — no image rendering needed
    if provider == "gemini":
        try:
            _t0 = time.monotonic()
            ranking = _rank_exercises_ai_gemini(exercise_pdf, answer_pdf, model, effort)
            print(f"  Ranking AI call (native PDF): {time.monotonic() - _t0:.1f}s")
            print(f"  Ranked {len(ranking)} question part(s).")
            return ranking
        except Exception as exc:
            print(f"  Ranking: native Gemini path failed ({exc}); falling back to image path.")

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

    def _call(messages: list[dict]) -> str:
        use_stream, thinking_kw = build_thinking_kwargs(provider, effort)
        print("  Waiting for AI response…", flush=True)
        if use_stream:
            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=True,
                **thinking_kw,
            )
            return collect_streamed_response(stream)
        completion = client.chat.completions.create(
            model=model,
            messages=messages,
            **thinking_kw,
        )
        return (completion.choices[0].message.content or "").strip()

    # First attempt: vision
    try:
        _t0 = time.monotonic()
        raw = _call(_build_vision_messages())
        print(f"  Ranking AI call: {time.monotonic() - _t0:.1f}s")
    except Exception as exc:
        print(f"  Ranking: vision call failed ({exc}); retrying with text.")
        try:
            _t0 = time.monotonic()
            raw = _call(_build_text_messages())
            print(f"  Ranking AI call (text fallback): {time.monotonic() - _t0:.1f}s")
        except Exception as exc2:
            print(f"  Ranking: text fallback also failed: {exc2}")
            return []

    ranking = _parse_ranking(raw)
    print(f"  Ranked {len(ranking)} question part(s).")
    return ranking


def _parse_ranking(response: str) -> list[str]:
    ranking: list[str] = []
    for line in response.strip().splitlines():
        cleaned = re.sub(r"^\s*\d+[\.\)]\s*", "", line).strip()
        if cleaned:
            ranking.append(cleaned)
    return ranking


# ---------------------------------------------------------------------------
# LaTeX generation
# ---------------------------------------------------------------------------

def _generate_ranking_latex(ranking: list[str], title: str) -> str:
    items = "\n".join(f"  \\item {_latex_escape(r)}" for r in ranking)
    escaped_title = _latex_escape(title)
    return rf"""\documentclass[12pt]{{article}}
\usepackage[a4paper, top=2.5cm, bottom=2.5cm, left=3cm, right=3cm]{{geometry}}
\usepackage[T1]{{fontenc}}
\usepackage[utf8]{{inputenc}}
\usepackage{{parskip}}
\usepackage{{enumitem}}
\begin{{document}}
\begin{{center}}
  {{\LARGE\bfseries Difficulty Ranking}}\\[0.5em]
  {{\large {escaped_title}}}\\[0.2em]
  {{\small most difficult $\rightarrow$ easiest}}
\end{{center}}
\vspace{{1.5em}}
\begin{{enumerate}}[leftmargin=2em]
{items}
\end{{enumerate}}
\end{{document}}
"""


# ---------------------------------------------------------------------------
# pdflatex helper
# ---------------------------------------------------------------------------

def _find_pdflatex() -> str | None:
    return shutil.which("pdflatex")


def _compile_ranking_latex(
    tex_source: str, out_pdf: Path, save_tex: Path | None = None
) -> bool:
    pdflatex = _find_pdflatex()
    if not pdflatex:
        print("  Skipping ranking PDF: pdflatex not found.")
        return False

    if save_tex is not None:
        save_tex.write_text(tex_source, encoding="utf-8")
        print(f"  Saved TeX: {save_tex}")

    with tempfile.TemporaryDirectory(prefix="ranking_") as tmp:
        tmp_path = Path(tmp)
        tex_file = tmp_path / "ranking.tex"
        tex_file.write_text(tex_source, encoding="utf-8")
        cmd = [
            pdflatex,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-output-directory", str(tmp_path),
            str(tex_file),
        ]
        for run in range(2):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    cwd=str(tmp_path),
                )
                if result.returncode != 0 and run == 1:
                    log = (result.stdout or "")[-1500:]
                    print(f"  Ranking: pdflatex failed:\n{log}")
                    return False
            except subprocess.TimeoutExpired:
                print("  Ranking: pdflatex timed out.")
                return False
            except OSError as exc:
                print(f"  Ranking: pdflatex error: {exc}")
                return False

        compiled = tmp_path / "ranking.pdf"
        if not compiled.is_file():
            print("  Ranking: pdflatex ran but produced no PDF.")
            return False

        shutil.copy2(str(compiled), str(out_pdf))
        return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_difficulty_ranking(
    exercise_pdf: Path,
    answer_pdf: Path | None,
    out_path: Path,
    name: str,
) -> Path | None:
    """Rank questions in *exercise_pdf* from hardest to easiest and save a PDF.

    Parameters
    ----------
    exercise_pdf:
        Path to the 1-up exercise sheet PDF.
    answer_pdf:
        Path to the 1-up answer sheet PDF, or ``None`` if unavailable.
    out_path:
        Directory where ``{name}_ranking.pdf`` will be written.
    name:
        Stem used as the PDF filename and document title.

    Returns the path to the saved PDF, or ``None`` if skipped/failed.
    """
    if os.environ.get("RANKING_SKIP", "").lower() in ("true", "1", "yes"):
        print("  Ranking skipped (RANKING_SKIP=true).")
        return None

    if not exercise_pdf.exists():
        print(f"  Ranking: exercise PDF not found: {exercise_pdf}")
        return None

    save_debug = os.environ.get("SAVE_TEX", "").lower() in ("true", "1", "yes")
    save_dir = out_path if save_debug else None

    print(f"  Calling AI for difficulty ranking ({name})…")
    try:
        ranking = _rank_exercises_ai(exercise_pdf, answer_pdf, save_dir=save_dir)
    except Exception as exc:
        print(f"  Ranking: unexpected error during AI call: {exc}")
        return None

    if not ranking:
        print("  Ranking: no ranking returned; skipping PDF generation.")
        return None

    tex = _generate_ranking_latex(ranking, name)
    dest = out_path / f"{name}_ranking.pdf"
    save_tex = out_path / f"{name}_ranking.tex" if save_debug else None

    print("  Compiling ranking PDF…", flush=True)
    try:
        ok = _compile_ranking_latex(tex, dest, save_tex=save_tex)
    except Exception as exc:
        print(f"  Ranking: LaTeX compilation error: {exc}")
        return None

    if ok:
        print(f"  Saved: {dest}")
        return dest
    return None
