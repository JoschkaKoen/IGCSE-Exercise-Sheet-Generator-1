"""Render a helper's markdown to HTML with KaTeX-compatible math passthrough.

Helpers (hint / solution / example / kb) are cached on disk as markdown and
emitted to the helper drawer as styled HTML. The browser still runs KaTeX
auto-render on the drawer, so we need the original ``$…$`` / ``$$…$$``
delimiters to survive intact in the output — markdown-it's emphasis rules
must not touch ``$x_1$``'s ``_1``.

The dollarmath plugin tokenises math at parse time, before emphasis rules
fire. We register a passthrough renderer that writes the original delimiters
back as text so KaTeX (still client-side) finds them in the DOM.
"""

from __future__ import annotations

from markdown_it import MarkdownIt
from mdit_py_plugins.dollarmath import dollarmath_plugin


def _math_passthrough(content: str, options: dict) -> str:
    """Emit math content back with its original ``$…$`` / ``$$…$$`` delimiters.

    markdown-it wraps the output in ``<span class="math inline">`` or
    ``<div class="math block">`` (for which we add scoped CSS). KaTeX
    auto-render walks the resulting text nodes and replaces the delimiters
    with rendered math.
    """
    if options.get("display_mode"):
        return f"$${content}$$"
    return f"${content}$"


_MD = (
    MarkdownIt("commonmark")
    .enable("table")
    .use(
        dollarmath_plugin,
        allow_labels=False,
        allow_space=True,
        allow_digits=True,
        double_inline=True,
        renderer=_math_passthrough,
    )
)


def render_helper_markdown(src: str) -> str:
    """Render ``src`` (markdown) to an HTML fragment ready for ``innerHTML``."""
    return _MD.render(src or "")
