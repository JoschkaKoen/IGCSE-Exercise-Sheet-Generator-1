# -*- coding: utf-8 -*-
"""AI-generated explanations for MCQ answers: text extraction → xAI → LaTeX → pdflatex → VectorStrips.

The public entry point is ``generate_mcq_explanation_strips``. It returns a list of
``VectorStrip`` objects (one per LaTeX output page) that slot directly into the
``layout_vector_strips_to_pdf`` pipeline, or ``[]`` on any failure so callers can
fall back to ``create_mcq_answer_strips``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import fitz

from .config import (
    A4_HEIGHT_PT,
    A4_WIDTH_PT,
    EXAM_LABEL_FONT_PT,
    EXAM_LABEL_TOP_PT,
    OUTPUT_MARGIN_PT,
    OUTPUT_MARGIN_RIGHT_PT,
    PROJECT_ROOT,
    SubjectConfig,
)

if TYPE_CHECKING:
    from .rendering import VectorStrip

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore[assignment,misc]

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None  # type: ignore[assignment]

_MARGIN_PT = float(OUTPUT_MARGIN_PT)
_MARGIN_RIGHT_PT = float(OUTPUT_MARGIN_RIGHT_PT)
_USABLE_W_PT = A4_WIDTH_PT - _MARGIN_PT - _MARGIN_RIGHT_PT

# Available height per output page for embedded strips (mirrors rendering.py constants).
# The header band occupies: LABEL_TOP + (LABEL_FS + 8) + LABEL_GAP = 10 + 17 + 6 = 33 pt.
_LABEL_H = float(EXAM_LABEL_FONT_PT) + 8.0
_LABEL_GAP_PT = 6.0
_OUTPUT_INITIAL_Y = float(EXAM_LABEL_TOP_PT) + _LABEL_H + _LABEL_GAP_PT  # ≈ 33 pt
_USABLE_H_PT = A4_HEIGHT_PT - _MARGIN_PT - _OUTPUT_INITIAL_Y  # ≈ 799 pt

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
}

_DEFAULT_SUBJECT_HINT = (
    "Use LaTeX $...$ for any inline mathematical expressions and $$...$$ for display equations."
)

# ---------------------------------------------------------------------------
# Step 1: extract question text from the QP
# ---------------------------------------------------------------------------


def extract_mcq_question_texts(
    doc: fitz.Document,
    regions: list[tuple[int, int, float, float]],
    questions: list[int],
    cfg: SubjectConfig,
) -> dict[int, str]:
    """Return plain text for each requested question, extracted from the clip region.

    Mirrors the portrait QP clip used by ``collect_vector_strips``:
    ``clip_x0 = cfg.strip_crop_left_pt``, ``clip_x1 = page_w - cfg.strip_crop_right_pt``,
    ``clip_y0 = y_start + cfg.strip_crop_top_pt``, ``clip_y1 = y_end``.

    Multi-page questions are concatenated with a space.
    """
    texts: dict[int, list[str]] = {q: [] for q in questions}
    for qnum, page_idx, y_start, y_end in regions:
        if qnum not in texts:
            continue
        if page_idx >= len(doc):
            continue
        page = doc[page_idx]
        page_w = page.rect.width
        clip = fitz.Rect(
            cfg.strip_crop_left_pt,
            y_start + cfg.strip_crop_top_pt,
            page_w - cfg.strip_crop_right_pt,
            y_end,
        )
        raw = page.get_text("text", clip=clip).strip()
        if raw:
            texts[qnum].append(raw)

    return {q: "\n".join(parts) for q, parts in texts.items() if parts}


# ---------------------------------------------------------------------------
# Step 2: call xAI API for explanations (one batch call)
# ---------------------------------------------------------------------------

_SYSTEM_TEMPLATE = """\
You are an expert Cambridge IGCSE {subject_title} tutor.

You will receive a list of multiple-choice questions with their correct answers.
For each question return exactly 3 concise bullet-point explanations.

Rules:
{subject_hint}
- Write in plain English, IGCSE level (age 14–16). Avoid unexplained jargon.
- Each bullet is 1–2 sentences maximum.
- Explain WHY the correct answer is right; briefly dismiss the most tempting distractor.
- Do NOT restate the question text. Do NOT say "the answer is X" — explain the reasoning.
- Output ONLY a valid JSON object, no markdown, no code fences:
  {{"explanations": {{"1": ["bullet1", "bullet2", "bullet3"], "2": ["...", "...", "..."], ...}}}}
- The keys are question numbers as strings. Every question number you receive must appear in the output.\
"""

_SUBJECT_TITLES: dict[str, str] = {
    "physics": "Physics",
    "mathematics": "Mathematics",
    "computer_science": "Computer Science",
}


def _build_system_prompt(exam_key: str | None) -> str:
    key = exam_key or ""
    title = _SUBJECT_TITLES.get(key, "Science")
    hint = _SUBJECT_HINTS.get(key, _DEFAULT_SUBJECT_HINT)
    return _SYSTEM_TEMPLATE.format(subject_title=title, subject_hint=hint)


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


def _parse_explanations(raw: str, questions: list[int]) -> dict[int, list[str]] | None:
    """Parse AI JSON; return dict or None on total failure.

    Accepts partial responses: questions missing from the response or with fewer
    than 3 bullets are padded with empty-string placeholders rather than dropped,
    so the template can still render a "(Explanation not available.)" for them.
    """
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    expl = data.get("explanations")
    if not isinstance(expl, dict):
        return None
    result: dict[int, list[str]] = {}
    for q in questions:
        v = expl.get(str(q))
        if isinstance(v, list) and len(v) >= 1 and all(isinstance(s, str) for s in v):
            bullets = [s.strip() for s in v[:3]]
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
) -> dict[int, list[str]]:
    """Call the AI once for all questions; return ``{qnum: [bullet, bullet, bullet]}``.

    Returns an empty dict on any error so the caller can fall back gracefully.
    """
    questions_with_answers = [q for q in questions if q in answers]
    if not questions_with_answers:
        return {}

    system = _build_system_prompt(exam_key)
    user = _build_user_message(q_texts, answers, questions_with_answers)

    def _call(**kwargs: Any) -> str:
        completion = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
        return (completion.choices[0].message.content or "").strip()

    for attempt in range(2):
        try:
            if attempt == 0:
                raw = _call(response_format={"type": "json_object"})
            else:
                # Some model/endpoint configs don't support json_object; retry without it
                raw = _call()
        except Exception as exc:
            print(f"  MCQ explanations: API error on attempt {attempt + 1}: {exc}")
            if attempt == 1:
                return {}
            continue

        result = _parse_explanations(raw, questions_with_answers)
        if result:
            return result

        print(f"  MCQ explanations: bad JSON on attempt {attempt + 1}, retrying…")
        # Nudge the model on retry
        user = (
            user
            + '\n\nYou MUST output ONLY valid JSON in this exact shape: '
            '{"explanations": {"1": ["...", "...", "..."], ...}}. '
            'No markdown, no code fences, no extra keys.'
        )

    return {}


# ---------------------------------------------------------------------------
# Batched AI workflow (prepare → one combined call → finalize per paper)
# ---------------------------------------------------------------------------


@dataclass
class McqPaperData:
    """All data needed to generate AI explanations for one MCQ paper, with no API call made yet."""

    qs: list[int]
    answers: dict[int, str]
    answered: list[int]
    q_texts: dict[int, str]
    exam_key: str | None
    paper_label: str
    expl_pdf_path: Path


def prepare_mcq_job_data(
    qp_doc: fitz.Document,
    regions: list[tuple[int, int, float, float]],
    answers: dict[int, str],
    qs: list[int],
    cfg: SubjectConfig,
    exam_key: str | None,
    paper_label: str,
    expl_pdf_path: Path,
) -> McqPaperData | None:
    """Extract question texts and assemble :class:`McqPaperData`. No API call is made.

    Returns ``None`` if there are no answered questions to process.
    """
    answered = [q for q in qs if q in answers]
    if not answered:
        return None
    q_texts = extract_mcq_question_texts(qp_doc, regions, qs, cfg)
    missing = [q for q in answered if q not in q_texts]
    if missing:
        print(f"  MCQ explanations: no text extracted for Q{missing} (will use placeholder).")
    return McqPaperData(
        qs=qs,
        answers=answers,
        answered=answered,
        q_texts=q_texts,
        exam_key=exam_key,
        paper_label=paper_label,
        expl_pdf_path=expl_pdf_path,
    )


def batch_generate_mcq_explanations(
    papers: list[McqPaperData],
) -> list[dict[int, list[str]]]:
    """Fire one focused AI call **per paper** in parallel threads.

    Each paper uses the same proven single-paper prompt that worked before,
    so the model always gives a reliable per-paper response.  The parallelism
    means N papers take roughly the same wall-clock time as a single paper.

    Returns one explanations dict per paper (in the same order as *papers*).
    Falls back to ``{}`` for any paper whose call fails, so the caller can
    render the plain-answer fallback strip via :func:`create_mcq_answer_strips`.
    """
    if not papers:
        return []
    client_model = _load_ai_client()
    if client_model is None:
        return [{} for _ in papers]
    client, model = client_model

    total_qs = sum(len(p.answered) for p in papers)
    print(
        f"  Calling AI for explanations "
        f"({len(papers)} paper(s), {total_qs} question(s) total, parallel)…"
    )

    def _call_one(paper: McqPaperData) -> dict[int, list[str]]:
        return generate_mcq_explanations(
            client, model,
            paper.q_texts, paper.answers, paper.answered, paper.exam_key,
        )

    results: list[dict[int, list[str]]] = [{} for _ in papers]
    with ThreadPoolExecutor(max_workers=len(papers)) as pool:
        future_to_idx = {pool.submit(_call_one, p): i for i, p in enumerate(papers)}
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as exc:
                print(f"  MCQ explanations: paper {idx} failed: {exc}")
    return results


def finalize_mcq_explanation_strips(
    job_data: McqPaperData,
    explanations: dict[int, list[str]],
) -> list[Any]:
    """Build LaTeX, compile, and convert to VectorStrips from pre-computed *explanations*.

    Returns ``[]`` if explanations are empty or compilation fails, so the caller can
    fall back to :func:`create_mcq_answer_strips`.
    """
    if not explanations:
        return []

    n_expl = len(explanations)
    print(f"  Received explanations for {n_expl}/{len(job_data.answered)} question(s).")

    tex = build_explanation_latex(job_data.qs, job_data.answers, explanations, job_data.paper_label)

    print("  Compiling LaTeX…")
    success = compile_latex(tex, job_data.expl_pdf_path)
    if not success:
        return []

    first_q = job_data.answered[0] if job_data.answered else None
    strips = _pdf_to_vector_strips(job_data.expl_pdf_path, job_data.answered, first_q)

    try:
        job_data.expl_pdf_path.unlink()
    except OSError:
        pass

    print(f"  MCQ explanation: {len(strips)} page(s) of explanations added.")
    return strips


# ---------------------------------------------------------------------------
# Step 3: build LaTeX source
# ---------------------------------------------------------------------------

_LATEX_SPECIAL = str.maketrans({
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "\\": r"\textbackslash{}",
})


def _latex_escape(text: str) -> str:
    """Escape LaTeX special characters in plain text (not in math modes)."""
    return text.translate(_LATEX_SPECIAL)


# Unicode → LaTeX replacements for characters the AI may output in bullets.
_UNICODE_TO_LATEX: list[tuple[str, str]] = [
    ("\u2014", "---"),    # em-dash
    ("\u2013", "--"),     # en-dash
    ("\u2018", "`"),      # left single quote
    ("\u2019", "'"),      # right single quote
    ("\u201c", "``"),     # left double quote
    ("\u201d", "''"),     # right double quote
    ("\u00d7", r"$\times$"),   # multiplication sign ×
    ("\u00b0", r"$^{\circ}$"), # degree sign °
    ("\u03b1", r"$\alpha$"),
    ("\u03b2", r"$\beta$"),
    ("\u03b3", r"$\gamma$"),
    ("\u03bb", r"$\lambda$"),
    ("\u03bc", r"$\mu$"),
    ("\u03c9", r"$\omega$"),
    ("\u03c6", r"$\phi$"),
    ("\u03c1", r"$\rho$"),
    ("\u03b8", r"$\theta$"),
    ("\u03c3", r"$\sigma$"),
    ("\u00b2", r"$^{2}$"),     # superscript 2
    ("\u00b3", r"$^{3}$"),     # superscript 3
    ("\u221a", r"$\sqrt{}$"),  # square root sign
    ("\u221e", r"$\infty$"),   # infinity
    ("\u2248", r"$\approx$"),  # approximately equal
    ("\u2260", r"$\neq$"),     # not equal
    ("\u2264", r"$\leq$"),     # less than or equal
    ("\u2265", r"$\geq$"),     # greater than or equal
    ("\u00b1", r"$\pm$"),      # plus-minus
    ("\u00bd", r"$\frac{1}{2}$"),  # one-half ½
    ("\u00bc", r"$\frac{1}{4}$"),  # one-quarter ¼
    ("\u00be", r"$\frac{3}{4}$"),  # three-quarters ¾
]


def _sanitize_bullet(text: str) -> str:
    """Replace common Unicode characters with LaTeX equivalents in AI-generated bullet text.

    AI bullet text already contains intentional $...$ math; we only replace Unicode
    chars that would cause pdflatex to fail or produce wrong output.
    """
    for ch, repl in _UNICODE_TO_LATEX:
        text = text.replace(ch, repl)
    return text


def _escape_question_text(raw: str) -> str:
    """Escape a question text for LaTeX, preserving math delimiters $...$ and $$...$$."""
    # Split on math regions; escape only the non-math parts.
    parts = re.split(r'(\$\$.*?\$\$|\$[^$]*?\$)', raw, flags=re.DOTALL)
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            out.append(_latex_escape(part))
        else:
            out.append(part)
    return "".join(out)


def _choose_pairs_per_row(n: int) -> int:
    """Return the number of Q/Ans pairs per row that divides *n* exactly.

    Tries values near 5 first so the table stays compact.  Falls back to 5
    (or n itself if n < 5) when no preferred divisor works.
    """
    for r in [5, 4, 6, 3, 7, 8, 10]:
        if r <= n and n % r == 0:
            return r
    return min(5, n)


def _build_answer_table(questions: list[int], answers: dict[int, str]) -> str:
    """Build a compact answer table with vertical rules between Q/Ans pairs.

    The number of pairs per row is chosen so every row is fully filled,
    eliminating any hanging separator on the last row.
    """
    rows: list[tuple[int, str]] = [(q, answers[q]) for q in questions if q in answers]
    if not rows:
        return ""

    # Pick pairs_per_row so len(rows) % pairs_per_row == 0 whenever possible.
    # >{\bfseries} requires \usepackage{array}.
    pairs_per_row = _choose_pairs_per_row(len(rows))
    pair_spec = r"r>{\bfseries}l"
    col_spec = r" | ".join([pair_spec] * pairs_per_row)
    col_arg = r"@{}" + col_spec + r"@{}"

    chunks = [rows[i:i + pairs_per_row] for i in range(0, len(rows), pairs_per_row)]

    lines = [
        r"\begin{tabular}{" + col_arg + "}",
        r"\toprule",
    ]
    for chunk in chunks:
        cells_list = [f"{q} & {a}" for q, a in chunk]
        missing = pairs_per_row - len(chunk)
        if missing:
            # Rare fallback: span unused columns as one invisible cell so LaTeX
            # does not raise a column-count mismatch error.
            cells_list.append(rf"\multicolumn{{{missing * 2}}}{{l}}{{}}")
        lines.append(" & ".join(cells_list) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines)


def build_explanation_latex(
    questions: list[int],
    answers: dict[int, str],
    explanations: dict[int, list[str]],
    paper_label: str,
) -> str:
    """Assemble the complete LaTeX source for the MCQ explanation document."""
    table = _build_answer_table(questions, answers)

    sections: list[str] = []
    for q in questions:
        if q not in answers:
            continue
        ans = answers[q]
        bullets = explanations.get(q)
        items = ""
        non_empty = [b for b in (bullets or []) if b.strip()]
        if non_empty:
            item_lines = "\n".join(
                r"  \item " + _sanitize_bullet(b) for b in non_empty
            )
            items = f"\\begin{{itemize}}[leftmargin=1.6em, itemsep=2pt, topsep=2pt, parsep=0pt]\n{item_lines}\n\\end{{itemize}}"
        else:
            items = r"\textit{(Explanation not available.)}"

        sections.append(
            f"\\vspace{{6pt}}\n"
            f"{{\\bfseries Question {q}\\enspace{{\\normalfont\\small (Answer: \\textbf{{{ans}}})}}}}\n\n"
            f"{items}"
        )

    escaped_label = _latex_escape(paper_label) if paper_label else "Multiple Choice"

    # Build the "Answers Q38–40:" side label (same bold 11pt as the Question headings below).
    answered_qs = [q for q in questions if q in answers]
    if answered_qs:
        q_min, q_max = min(answered_qs), max(answered_qs)
        q_range = f"Q{q_min}" if q_min == q_max else f"Q{q_min}--{q_max}"
        side_label = rf"\bfseries Answers {q_range}:"
    else:
        side_label = r"\bfseries Answers:"

    # \hfill TABLE \hfill\phantom{label} centres the table on the full linewidth:
    # the phantom mirrors the label's width on the right so both \hfills are equal.
    header_row = (
        rf"\noindent {{{side_label}}}"
        r"\hfill"
        "\n"
        f"{table}"
        "\n"
        rf"\hfill\phantom{{{side_label}}}"
    )

    body = "\n\n".join(sections)

    return rf"""\documentclass[11pt]{{article}}
\usepackage[utf8]{{inputenc}}
\usepackage[T1]{{fontenc}}
\usepackage[a4paper, top=0.5cm, bottom=1.6cm, left=2cm, right=2cm]{{geometry}}
\usepackage{{amsmath, amssymb}}
\usepackage{{array}}
\usepackage{{booktabs}}
\usepackage[shortlabels]{{enumitem}}
\usepackage{{parskip}}
\usepackage{{lmodern}}
\usepackage{{microtype}}
\usepackage{{xcolor}}

\setlength{{\parindent}}{{0pt}}
\setlength{{\parskip}}{{4pt}}
\setlength{{\topskip}}{{0pt}}
\pagestyle{{empty}}

\begin{{document}}

{header_row}

\vspace{{4pt}}

{body}

\end{{document}}
"""


# ---------------------------------------------------------------------------
# Step 4: compile with pdflatex
# ---------------------------------------------------------------------------


def _find_pdflatex() -> str | None:
    """Return path to pdflatex, preferring MacTeX location."""
    candidates = [
        "/Library/TeX/texbin/pdflatex",
        "/usr/local/bin/pdflatex",
        "/usr/bin/pdflatex",
    ]
    for c in candidates:
        if Path(c).is_file():
            return c
    return shutil.which("pdflatex")


def compile_latex(tex_source: str, output_pdf: Path) -> bool:
    """Write *tex_source* to a temp dir, run pdflatex, copy result to *output_pdf*.

    Returns ``True`` on success, ``False`` on failure.
    """
    pdflatex = _find_pdflatex()
    if not pdflatex:
        print("  MCQ explanations: pdflatex not found; falling back to plain text.")
        return False

    with tempfile.TemporaryDirectory(prefix="mcq_expl_") as tmp:
        tmp_path = Path(tmp)
        tex_file = tmp_path / "explanations.tex"
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
                    log_snippet = (result.stdout or "")[-1500:]
                    print(f"  MCQ explanations: pdflatex failed (run {run + 1}):\n{log_snippet}")
                    return False
            except subprocess.TimeoutExpired:
                print("  MCQ explanations: pdflatex timed out.")
                return False
            except OSError as exc:
                print(f"  MCQ explanations: pdflatex error: {exc}")
                return False

        compiled = tmp_path / "explanations.pdf"
        if not compiled.is_file():
            print("  MCQ explanations: pdflatex ran but produced no PDF.")
            return False

        shutil.copy2(str(compiled), str(output_pdf))
        return True


# ---------------------------------------------------------------------------
# Step 5: convert compiled PDF pages to VectorStrips
# ---------------------------------------------------------------------------


def _content_bottom_pt(page: fitz.Page, padding: float = 6.0) -> float:
    """Return the y-coordinate of the bottom of the last piece of content on *page*.

    Scans text blocks and vector drawings (e.g. booktabs rules) to find the
    lowest ink on the page, then adds *padding* points of breathing room.
    The result is capped at the page height so it is always a valid clip limit.
    """
    max_y = 0.0
    for block in page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]:
        max_y = max(max_y, block["bbox"][3])
    for d in page.get_drawings():
        max_y = max(max_y, d["rect"].y1)
    return min(max_y + padding, page.rect.height)


def _pdf_to_vector_strips(
    pdf_path: Path,
    questions_in_order: list[int],
    first_q_num: int | None,
) -> list[Any]:
    """Open *pdf_path* and return one VectorStrip per page, keeping the doc open.

    The returned doc is embedded in the strips and must stay alive as long as the
    strips are used by the layout engine.  The caller (pipeline) holds a reference
    via the returned strips list.

    ``question_num`` is set to ``first_q_num`` on the first strip so the overview
    anchor is recorded; subsequent pages get None.
    """
    # Defer rendering import to avoid circular imports
    from .rendering import VectorStrip  # noqa: PLC0415

    # Open from bytes so the file handle is not kept alive and the caller can
    # delete the file on disk straight after this function returns.
    doc = fitz.open(stream=pdf_path.read_bytes(), filetype="pdf")
    strips: list[Any] = []
    for page_idx in range(len(doc)):
        page = doc[page_idx]
        pr = page.rect
        # Tight-crop: only claim vertical space down to the last line of content.
        # This lets the layout engine pack subsequent content (other MCQ papers,
        # structured mark schemes) onto the same output page instead of leaving gaps.
        content_h = _content_bottom_pt(page)

        scale_w = _USABLE_W_PT / pr.width if pr.width > 0 else 1.0
        scale_h = _USABLE_H_PT / content_h if content_h > 0 else 1.0
        scale = min(scale_w, scale_h)
        display_w = pr.width * scale
        display_h = content_h * scale
        strips.append(VectorStrip(
            src_doc=doc,
            page_idx=page_idx,
            clip_rect=fitz.Rect(0, 0, pr.width, content_h),
            display_h_pt=display_h,
            display_w_pt=display_w,
            x_offset_pt=(_USABLE_W_PT - display_w) / 2 + _MARGIN_PT,
            qr_rects=[],
            question_num=first_q_num if page_idx == 0 else None,
        ))
    return strips


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _load_ai_client() -> tuple[Any, str] | None:
    """Load xAI client from environment; return (client, model) or None."""
    if OpenAI is None:
        print("  MCQ explanations: openai package not installed.")
        return None

    if load_dotenv is not None:
        load_dotenv(PROJECT_ROOT / ".env")
        load_dotenv(Path.cwd() / ".env")

    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        print("  MCQ explanations: XAI_API_KEY not set; skipping AI explanations.")
        return None

    model = os.environ.get("XAI_MCQ_MODEL") or os.environ.get("XAI_MODEL", "grok-4-1-fast-non-reasoning")
    try:
        client = OpenAI(api_key=api_key, base_url="https://api.x.ai/v1")
    except Exception as exc:
        print(f"  MCQ explanations: failed to create AI client: {exc}")
        return None
    return client, model


def generate_mcq_explanation_strips(
    qp_doc: fitz.Document,
    regions: list[tuple[int, int, float, float]],
    answers: dict[int, str],
    qs: list[int],
    cfg: SubjectConfig,
    exam_key: str | None,
    paper_label: str,
    expl_pdf_path: Path,
) -> list[Any]:
    """Generate AI explanation strips for the given MCQ job.

    Returns a list of ``VectorStrip`` objects (one per LaTeX output page) on
    success, or ``[]`` on any failure.

    Parameters
    ----------
    qp_doc:
        Open PyMuPDF document for the question paper.
    regions:
        ``(qnum, page_idx, y_start, y_end)`` tuples from ``get_question_regions``.
    answers:
        ``{qnum: letter}`` from ``parse_mcq_answers``.
    qs:
        The requested question numbers for this job.
    cfg:
        Subject config (for clip rect constants).
    exam_key:
        ``"physics"``, ``"computer_science"``, ``"mathematics"``, or ``None``.
    paper_label:
        Human-readable label for the paper (used in the LaTeX title).
    expl_pdf_path:
        Where to write the compiled PDF (inside the run's output dir).
    """
    print("  Generating AI explanations for MCQ answers…")

    job_data = prepare_mcq_job_data(
        qp_doc=qp_doc,
        regions=regions,
        answers=answers,
        qs=qs,
        cfg=cfg,
        exam_key=exam_key,
        paper_label=paper_label,
        expl_pdf_path=expl_pdf_path,
    )
    if job_data is None:
        return []

    [explanations] = batch_generate_mcq_explanations([job_data])
    return finalize_mcq_explanation_strips(job_data, explanations)
