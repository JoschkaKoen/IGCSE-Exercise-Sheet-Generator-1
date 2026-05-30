# -*- coding: utf-8 -*-
"""Markdown handout → xelatex document converter (deterministic, no AI).

Turns an authored handout ``output/eXam/handouts/<subject>/<NN>.md`` into a
standalone xelatex ``.tex`` that compiles to a print-quality PDF. The website
keeps rendering the *live* markdown (``eXam/render_helper.py`` → HTML + KaTeX);
this is a separate print path.

Parsing reuses the exact ``markdown-it`` + ``dollarmath`` construction the site
uses (``eXam/render_helper.py``) but walks the token stream and emits LaTeX
instead of rendering HTML. The dollarmath plugin already isolates ``$…$`` /
``$$…$$`` as their own tokens, so math/glosses are never mangled by emphasis or
escaping. Math and code-fence content pass through **raw**; only ``text`` /
``code_inline`` get LaTeX escaping. Figures are swapped from the 300 dpi web
``.png`` to the vector ``.pdf`` crop (or 600 dpi ``.print.png``) for print.

CJK + preamble follow the user's previous-semester xelatex handouts: fontspec
defaults (Latin Modern — covers Latin, ``×``/``°`` and the ``←``/``→`` that
appear in code), ``[boldfont=false]{xeCJK}`` (CJK appears inside bold headings),
and the TeX Live-bundled ``FandolSong-Regular.otf`` (host-independent).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin

from .handouts_collect import HANDOUTS_ROOT

__all__ = [
    "build_document",
    "render_body",
    "build_preamble",
    "resolve_print_image",
    "escape_latex",
    "CJK_MAIN_FONT",
]

# TeX Live-bundled (texlive-lang-cjk); resolves by filename via kpathsea on both
# macOS MacTeX and Debian — no system font / fontconfig dependency.
CJK_MAIN_FONT = "FandolSong-Regular.otf"

# Same construction as eXam/render_helper.py:33-44, but the math renderer is a
# no-op (we read token.content directly when walking, not .render()).
_MD = (
    MarkdownIt("commonmark")
    .enable("table")
    .use(
        dollarmath_plugin,
        allow_labels=False,
        allow_space=True,
        allow_digits=True,
        double_inline=True,
        renderer=lambda content, options: content,
    )
)

# Mirror of xscore/marking/report_latex_text.py:20-39 (kept local to avoid a
# web → xscore.marking private-symbol import). 12 entries.
_LATEX_MAP = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "<": r"\textless{}",
    ">": r"\textgreater{}",
}
_LATEX_RE = re.compile("|".join(re.escape(k) for k in _LATEX_MAP))

_HEADING_CMD = {
    "h1": "section",
    "h2": "section",
    "h3": "subsection",
    "h4": "subsubsection",
    "h5": "paragraph",
    "h6": "subparagraph",
}


def _esc(text: str) -> str:
    """Escape LaTeX specials in prose text (single pass). Unicode (CJK, ×, °)
    passes through untouched — xelatex renders it directly."""
    return _LATEX_RE.sub(lambda m: _LATEX_MAP[m.group()], text)


# Public alias: ``web.vocab_latex`` escapes glossary cells with the same single-pass
# escaper (CJK / pinyin diacritics pass through untouched).
escape_latex = _esc


def _subject_display(subject_key: str) -> str:
    return subject_key.replace("_", " ").title()


def resolve_print_image(url: str, *, subject: str) -> Path | None:
    """Map a ``/handout-media/...png`` URL to the best on-disk print file.

    Prefers the vector ``.pdf`` crop, then the 600 dpi ``.print.png``, then the
    300 dpi web ``.png``. Returns ``None`` if nothing resolves or the path would
    escape the handouts tree. (``subject`` is accepted for symmetry / future
    validation; the URL already carries the subject segment.)
    """
    prefix = "/handout-media/"
    if not url.startswith(prefix):
        return None
    base = (HANDOUTS_ROOT / url[len(prefix):]).resolve()
    try:
        base.relative_to(HANDOUTS_ROOT.resolve())
    except ValueError:
        return None
    if base.suffix.lower() != ".png":
        return base if base.is_file() else None
    stem = str(base)[: -len(".png")]
    for cand in (Path(stem + ".pdf"), Path(stem + ".print.png"), base):
        if cand.is_file():
            return cand
    return None


class _Renderer:
    """Walks the markdown-it token stream and emits LaTeX into ``self.out``."""

    def __init__(self, subject: str, meta: dict[str, Any]):
        self.subject = subject
        self.meta = meta
        self.out: list[str] = []
        self.warnings: list[str] = []

    # ── inline (returns a string; used by blocks and table cells) ──────────
    def inline(self, children: list[Any] | None) -> str:
        parts: list[str] = []
        for t in children or []:
            ty = t.type
            if ty == "text":
                parts.append(_esc(t.content))
            elif ty == "strong_open":
                parts.append(r"\textbf{")
            elif ty == "em_open":
                parts.append(r"\emph{")
            elif ty in ("strong_close", "em_close"):
                parts.append("}")
            elif ty == "code_inline":
                parts.append(r"\texttt{" + _esc(t.content) + "}")
            elif ty == "math_inline":
                parts.append("$" + t.content + "$")
            elif ty == "math_inline_double":
                parts.append(r"\[" + t.content + r"\]")
            elif ty == "softbreak":
                parts.append(" ")
            elif ty == "hardbreak":
                parts.append("\\\\\n")
            elif ty == "image":
                # Inline image not at paragraph start (rare; corpus figures are
                # their own paragraph). Embed small, inline.
                p = resolve_print_image(t.attrGet("src") or "", subject=self.subject)
                if p is not None:
                    parts.append(r"\includegraphics[height=1em]{" + str(p) + "}")
            elif ty in ("link_open", "link_close"):
                pass  # drop the href, keep the link text
            elif t.content:
                parts.append(_esc(t.content))
        return "".join(parts)

    # ── blocks (append to self.out; recurse over a flat token range) ───────
    def blocks(self, tokens: list[Any], lo: int, hi: int) -> None:
        i = lo
        while i < hi:
            t = tokens[i]
            ty = t.type
            if ty == "heading_open":
                cmd = _HEADING_CMD.get(t.tag, "section")
                self.out.append(f"\\{cmd}*{{{self.inline(tokens[i + 1].children)}}}\n\n")
                i += 3
            elif ty == "paragraph_open":
                self._paragraph(tokens[i + 1])
                i += 3
            elif ty in ("bullet_list_open", "ordered_list_open"):
                env = "itemize" if ty.startswith("bullet") else "enumerate"
                j = _matching_close(tokens, i)
                self.out.append(f"\\begin{{{env}}}\n")
                self.blocks(tokens, i + 1, j)
                self.out.append(f"\\end{{{env}}}\n\n")
                i = j + 1
            elif ty == "list_item_open":
                j = _matching_close(tokens, i)
                self.out.append(r"\item ")
                self.blocks(tokens, i + 1, j)
                i = j + 1
            elif ty == "table_open":
                j = _matching_close(tokens, i)
                self._table(tokens, i, j)
                i = j + 1
            elif ty == "math_block":
                self.out.append("\\[\n" + t.content.strip() + "\n\\]\n\n")
                i += 1
            elif ty in ("fence", "code_block"):
                body = t.content if t.content.endswith("\n") else t.content + "\n"
                self.out.append("\\begin{Verbatim}\n" + body + "\\end{Verbatim}\n\n")
                i += 1
            elif ty == "blockquote_open":
                j = _matching_close(tokens, i)
                self.out.append("\\begin{quote}\n")
                self.blocks(tokens, i + 1, j)
                self.out.append("\\end{quote}\n\n")
                i = j + 1
            elif ty == "hr":
                self.out.append("\\par\\noindent\\rule{\\linewidth}{0.4pt}\\par\n\n")
                i += 1
            elif ty == "html_block":
                self.out.append(_esc(t.content) + "\n\n")
                i += 1
            else:
                i += 1

    def _paragraph(self, inline_tok: Any) -> None:
        children = inline_tok.children or []
        if children and children[0].type == "image":
            self._figure(children)
        else:
            self.out.append(self.inline(children) + "\n\n")

    def _figure(self, children: list[Any]) -> None:
        img = children[0]
        src = img.attrGet("src") or ""
        path = resolve_print_image(src, subject=self.subject)
        if path is None:
            self.warnings.append(f"figure missing/unresolved: {src}")
            return
        name = path.name
        if name.lower().endswith(".png") and not name.endswith(".print.png"):
            self.warnings.append(f"figure only 300dpi (no vector/print sibling): {name}")
        caption = self.inline(children[1:]).strip()
        # minipage keeps the image and its caption together — a plain center block
        # is page-breakable and can strand the caption on the next page.
        self.out.append("\\par\\medskip\n\\noindent\\begin{minipage}{\\linewidth}\n\\centering\n")
        self.out.append(
            "\\adjustbox{max width=\\linewidth, max totalheight=0.42\\textheight}{%\n"
            f"  \\includegraphics{{{path}}}}}"
        )
        if caption:
            self.out.append("\\\\[0.3em]\n" + caption)
        self.out.append("\n\\end{minipage}\\par\\medskip\n\n")

    def _table(self, tokens: list[Any], i: int, j: int) -> None:
        aligns: list[str] = []
        header: list[str] = []
        rows: list[list[str]] = []
        cur: list[str] | None = None
        cur_is_head = False
        in_head = False
        for k in range(i, j + 1):
            t = tokens[k]
            ty = t.type
            if ty == "thead_open":
                in_head = True
            elif ty == "thead_close":
                in_head = False
            elif ty == "tr_open":
                cur = []
                cur_is_head = in_head
            elif ty == "tr_close":
                if cur is not None:
                    (header.extend(cur) if cur_is_head else rows.append(cur))
                cur = None
            elif ty == "th_open":
                style = t.attrGet("style") or ""
                aligns.append("r" if "right" in style else "c" if "center" in style else "l")
            elif ty == "inline":
                if cur is not None:
                    cur.append(self.inline(t.children))
        ncol = len(header) or (len(rows[0]) if rows else 1)
        if len(aligns) < ncol:
            aligns += ["l"] * (ncol - len(aligns))
        colspec = "".join(aligns[:ncol])
        self.out.append("\\begin{center}\n\\adjustbox{max width=\\linewidth}{%\n")
        self.out.append("\\begin{tabular}{" + colspec + "}\n\\toprule\n")
        if header:
            self.out.append(" & ".join(r"\textbf{" + c + "}" for c in header) + " \\\\\n\\midrule\n")
        for row in rows:
            self.out.append(" & ".join(row) + " \\\\\n")
        self.out.append("\\bottomrule\n\\end{tabular}}\n\\end{center}\n\n")


def _matching_close(tokens: list[Any], i: int) -> int:
    """Index of the ``*_close`` matching the ``*_open`` at ``i`` (nesting-aware)."""
    open_type = tokens[i].type
    close_type = open_type.replace("_open", "_close")
    depth = 0
    for k in range(i, len(tokens)):
        if tokens[k].type == open_type:
            depth += 1
        elif tokens[k].type == close_type:
            depth -= 1
            if depth == 0:
                return k
    return len(tokens) - 1


def build_preamble() -> str:
    """xelatex preamble — fontspec defaults (Latin Modern) + Fandol CJK, modeled
    on the user's previous-semester handouts. No ``\\setmainfont``/``\\setmonofont``
    (the LM defaults cover Latin, ×, °, and the ←/→ used in code)."""
    return "\n".join(
        [
            r"\documentclass[11pt]{article}",
            r"\usepackage[a4paper,margin=1in]{geometry}",
            r"\usepackage{fontspec}",
            r"\usepackage[boldfont=false]{xeCJK}",
            rf"\setCJKmainfont{{{CJK_MAIN_FONT}}}",
            r"\usepackage{amsmath}",
            r"\usepackage{amssymb}",
            r"\usepackage{array}",
            r"\usepackage{booktabs}",
            r"\usepackage{graphicx}",
            r"\usepackage{adjustbox}",
            r"\usepackage{enumitem}",
            r"\setlist[itemize]{topsep=2pt,partopsep=0pt,parsep=0pt,itemsep=2pt,leftmargin=1.4em}",
            r"\setlist[enumerate]{topsep=2pt,partopsep=0pt,parsep=0pt,itemsep=2pt,leftmargin=1.6em}",
            r"\usepackage{parskip}",
            r"\usepackage{fvextra}",
            r"\fvset{breaklines=true,breakanywhere=true,fontsize=\small}",
            r"\setlength{\parindent}{0pt}",
        ]
    )


def render_body(md_src: str, *, subject: str, meta: dict[str, Any]) -> tuple[str, list[str]]:
    """Markdown → LaTeX body (no preamble). Returns (latex, warnings)."""
    r = _Renderer(subject, meta or {})
    tokens = _MD.parse(md_src or "")
    r.blocks(tokens, 0, len(tokens))
    return "".join(r.out), r.warnings


def build_document(
    md_src: str, *, subject: str, topic: str, meta: dict[str, Any]
) -> tuple[str, list[str]]:
    """Full standalone ``.tex`` (preamble + title block + body). Returns (tex, warnings)."""
    meta = meta or {}
    title = str(meta.get("topic_title") or f"Topic {topic}")
    body, warnings = render_body(md_src, subject=subject, meta=meta)
    title_block = (
        "\\begin{center}\n"
        f"  {{\\LARGE\\bfseries {_esc(title)}}}\\\\[0.35em]\n"
        f"  {{\\large {_esc(_subject_display(subject))}}}\n"
        "\\end{center}\n\\vspace{0.6em}\n\n"
    )
    tex = (
        build_preamble()
        + "\n\n\\begin{document}\n\n"
        + title_block
        + body
        + "\n\\end{document}\n"
    )
    return tex, warnings
