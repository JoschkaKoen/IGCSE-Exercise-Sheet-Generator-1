"""Verification PDF for extracted mark-scheme graphics.

Side-artifact rendered as part of class_report (``class_report``). Walks the
transcriptions YAML produced by transcribe_scheme_graphics and emits a single PDF with one page
per graphic — header, image, and the AI's transcription text — so the user
can visually confirm both extraction (detect_mark_scheme_graphics) and transcription (transcribe_scheme_graphics)
worked correctly.

Pure side-artifact: never read by another step; safe to delete or skip.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

import yaml

from xscore.marking.class_report import _compile_tex
from xscore.marking.report_latex import _ENV
from xscore.marking.report_latex_text import _latex_escape
from xscore.shared.path_builders import (
    artifact_mark_scheme_graphics_dir,
    artifact_scheme_graphic_transcriptions_path,
    artifact_scheme_graphics_check_pdf_path,
    artifact_scheme_graphics_check_tex_path,
)
from xscore.shared.terminal_ui import warn_line


_NATKEY_RE = re.compile(r"(\d+)")


def _natural_qnum_key(s: str) -> list:
    """Sort key so '7a' < '10' (digit chunks compared as ints)."""
    return [int(t) if t.isdigit() else t for t in _NATKEY_RE.split(s)]


def _sort_key(entry: dict) -> tuple:
    return (
        int(entry.get("ms_page") or 0),
        _natural_qnum_key(str(entry.get("question_number") or "")),
        int(entry.get("graphic_index") or 0),
    )


def _load_entries(artifact_dir: Path) -> list[dict]:
    """Load transcriptions.yaml; drop entries whose PNG no longer exists."""
    yaml_path = artifact_scheme_graphic_transcriptions_path(artifact_dir)
    if not yaml_path.is_file():
        return []
    try:
        doc = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        warn_line(f"scheme graphics check: could not read {yaml_path.name}: {exc}")
        return []
    raw = doc.get("graphics") or []
    if not isinstance(raw, list):
        return []
    graphics_dir = artifact_mark_scheme_graphics_dir(artifact_dir)
    out: list[dict] = []
    for e in raw:
        if not isinstance(e, dict):
            continue
        fname = str(e.get("file") or "")
        if not fname or not (graphics_dir / fname).is_file():
            if fname:
                warn_line(f"scheme graphics check: PNG missing — skipping {fname}")
            continue
        out.append(e)
    out.sort(key=_sort_key)
    return out


def _format_transcription(text: str) -> str:
    """Escape LaTeX specials, then preserve newlines: blank line = paragraph
    break, single newline = forced line break (\\\\). Empty input renders an
    italic placeholder so the page never looks blank."""
    text = (text or "").strip()
    if not text:
        return r"\textit{(no transcription captured)}"
    escaped = _latex_escape(text)
    paragraphs = re.split(r"\n\s*\n", escaped)
    return "\n\n".join(p.replace("\n", "\\\\\n") for p in paragraphs)


def _build_body_tex(entries: list[dict]) -> str:
    """Concatenate per-entry TeX blocks, with \\newpage BETWEEN entries (not
    after the last) so the PDF doesn't end on an empty trailing page."""
    blocks: list[str] = []
    for e in entries:
        ms_page = e.get("ms_page", "?")
        qnum = _latex_escape(str(e.get("question_number") or "?"))
        fname = str(e.get("file") or "")
        body = _format_transcription(str(e.get("transcription") or ""))
        blocks.append(
            f"\\section*{{Mark Scheme Page {ms_page}  ·  Question {qnum}}}\n"
            f"\\begin{{center}}\n"
            f"\\includegraphics[max width=\\linewidth,"
            f"max totalheight=0.55\\textheight]{{{Path(fname).stem}}}\n"
            f"\\end{{center}}\n"
            f"\\vspace{{0.6em}}\n"
            f"\\textbf{{Transcription:}}\\par\n"
            f"\\vspace{{0.2em}}\n"
            f"{body}\n"
        )
    return "\n\\newpage\n".join(blocks)


def render_scheme_graphics_check_pdf(ctx: Any) -> str:
    """Build a verification PDF: one page per extracted mark-scheme graphic,
    showing the graphic + its transcription, sorted by ms_page then question.

    Returns ``"done"`` | ``"skipped_empty"`` | ``"skipped_missing"``. Never
    raises — errors become ``warn_line`` so class_report's main output is unaffected.
    """
    artifact_dir: Path = ctx.artifact_dir
    try:
        entries = _load_entries(artifact_dir)
        if not entries:
            return "skipped_empty"

        scheme_dir = str(artifact_mark_scheme_graphics_dir(artifact_dir).resolve()) + "/"
        tex_path = artifact_scheme_graphics_check_tex_path(artifact_dir)
        pdf_path = artifact_scheme_graphics_check_pdf_path(artifact_dir)
        tex_path.parent.mkdir(parents=True, exist_ok=True)

        rendered = _ENV.get_template("scheme_graphics_check.tex.j2").render(
            scheme_graphics_dir=scheme_dir,
            date_str=datetime.date.today().isoformat(),
            n_graphics=len(entries),
            body=_build_body_tex(entries),
        )
        tex_path.write_text(rendered, encoding="utf-8")
        _compile_tex(tex_path, tex_path.parent)
        return "done" if pdf_path.is_file() else "skipped_missing"
    except Exception as exc:  # noqa: BLE001
        warn_line(f"scheme graphics check failed: {type(exc).__name__}: {exc}")
        return "skipped_missing"
