# -*- coding: utf-8 -*-
"""Shared LaTeX text-escaping utilities.

Used by mcq_explanations and difficulty_ranking. Keep this module
dependency-free (no internal imports) so it can be imported early.
"""

from __future__ import annotations

# Characters that must be escaped in LaTeX plain-text contexts.
_LATEX_SPECIAL = str.maketrans({
    "\\": r"\textbackslash{}",
    "{":  r"\{",
    "}":  r"\}",
    "$":  r"\$",
    "#":  r"\#",
    "%":  r"\%",
    "&":  r"\&",
    "_":  r"\_",
    "~":  r"\textasciitilde{}",
    "^":  r"\textasciicircum{}",
})


def latex_escape(text: str) -> str:
    """Escape LaTeX special characters in plain text (not inside math modes)."""
    return text.translate(_LATEX_SPECIAL)


# Unicode → LaTeX replacements for characters the AI may output in bullets.
_UNICODE_TO_LATEX: list[tuple[str, str]] = [
    ("\u2014", "---"),               # em-dash
    ("\u2013", "--"),                # en-dash
    ("\u2018", "`"),                 # left single quote
    ("\u2019", "'"),                 # right single quote
    ("\u201c", "``"),                # left double quote
    ("\u201d", "''"),                # right double quote
    ("\u00d7", r"$\times$"),         # multiplication sign ×
    ("\u00b0", r"$^{\circ}$"),       # degree sign °
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
    ("\u00b2", r"$^{2}$"),           # superscript 2
    ("\u00b3", r"$^{3}$"),           # superscript 3
    ("\u221a", r"$\sqrt{}$"),        # square root sign
    ("\u221e", r"$\infty$"),         # infinity
    ("\u2248", r"$\approx$"),        # approximately equal
    ("\u2260", r"$\neq$"),           # not equal
    ("\u2264", r"$\leq$"),           # less than or equal
    ("\u2265", r"$\geq$"),           # greater than or equal
    ("\u00b1", r"$\pm$"),            # plus-minus
    ("\u00bd", r"$\frac{1}{2}$"),    # one-half ½
    ("\u00bc", r"$\frac{1}{4}$"),    # one-quarter ¼
    ("\u00be", r"$\frac{3}{4}$"),    # three-quarters ¾
]


def sanitize_bullet(text: str) -> str:
    """Replace common Unicode characters with LaTeX equivalents in AI-generated bullet text.

    AI bullet text may already contain intentional $...$ math; only Unicode
    chars that would cause pdflatex to fail or produce wrong output are replaced.
    """
    for ch, repl in _UNICODE_TO_LATEX:
        text = text.replace(ch, repl)
    return text


