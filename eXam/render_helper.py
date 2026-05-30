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

# The default image renderer is a bound method on the renderer; capture it so the
# override below can delegate after (optionally) setting size attributes.
_default_image_rule = _MD.renderer.rules["image"]


def _image_with_optional_dims(tokens, idx, options, env):
    """Set ``<img width/height>`` from an env-supplied ``img_dims(src) -> (w, h) | None``.

    A strict no-op when ``env`` lacks the resolver, so every caller that renders without
    one (``/code`` lessons, eXam helper drawers, exam-question text) is byte-for-byte
    unchanged. Attributes are set on the bare ``<img>`` token — no wrapper — so the
    ``img + em`` caption CSS keeps matching.
    """
    tok = tokens[idx]
    resolver = (env or {}).get("img_dims")
    if resolver is not None and tok.attrGet("width") is None:
        dims = resolver(tok.attrGet("src"))
        if dims:
            tok.attrSet("width", str(dims[0]))
            tok.attrSet("height", str(dims[1]))
    return _default_image_rule(tokens, idx, options, env)


_MD.renderer.rules["image"] = _image_with_optional_dims


def render_helper_markdown(src: str, env: dict | None = None) -> str:
    """Render ``src`` (markdown) to an HTML fragment ready for ``innerHTML``.

    ``env`` is forwarded to markdown-it; pass ``{"img_dims": resolver}`` to size ``<img>``
    tags at render time (see ``web.routes.learn``). Omitted by every other caller.
    """
    return _MD.render(src or "", env)
