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

Rules:
- If a question has sub-parts (a, b, c) or sub-sub-parts (a(i), a(ii)), rank each
  part individually. If a question has no sub-parts, rank the whole question.
- For multi-paper sheets, prefix each ID with the paper label, e.g. "w24/21 Q7a".
  For single-paper sheets, use just the identifier, e.g. "7a".
- Output ONLY a plain numbered list, one identifier per line. No prose, no headings.
- Example output:
  1. 12a(i)
  2. 7b
  3. 3\
"""


def _rank_exercises_ai(
    exercise_pdf: Path,
    answer_pdf: Path | None,
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
        content: list[dict] = []
        content.append({"type": "text", "text": "=== EXERCISE SHEET ==="})
        for img in ex_images:
            content.append({"type": "image_url", "image_url": {"url": img}})
        if ans_images:
            print(f"    Answer sheet: {len(ans_images)} page(s)")
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
    items = "\n".join(f"  \\item Ex.~{_latex_escape(r)}" for r in ranking)
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

    print(f"  Calling AI for difficulty ranking ({name})…")
    try:
        ranking = _rank_exercises_ai(exercise_pdf, answer_pdf)
    except Exception as exc:
        print(f"  Ranking: unexpected error during AI call: {exc}")
        return None

    if not ranking:
        print("  Ranking: no ranking returned; skipping PDF generation.")
        return None

    tex = _generate_ranking_latex(ranking, name)
    dest = out_path / f"{name}_ranking.pdf"
    save_tex = (
        out_path / f"{name}_ranking.tex"
        if os.environ.get("SAVE_TEX", "").lower() in ("true", "1", "yes")
        else None
    )

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
