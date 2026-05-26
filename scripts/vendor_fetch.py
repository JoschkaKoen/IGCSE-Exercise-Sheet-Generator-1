#!/usr/bin/env python3
"""Download external CDN assets used by web templates into web/static/vendor/.

Run once to populate web/static/vendor/, then commit the result. Re-run to
refresh (always overwrites). Uses stdlib only.

Resources fetched:
  - Tailwind Play CDN (single JS file)
  - Google Fonts: Outfit + Sora (CSS + woff2)
  - Twemoji country-flag polyfill (CSS + woff2)
  - KaTeX 0.16.11 (CSS + JS + auto-render + ~20 font files)
  - pdfjs-dist 4.6.82 (pdf.mjs + pdf.worker.mjs)
  - Chart.js 4.4.6 (UMD min)
"""
from __future__ import annotations

import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = REPO_ROOT / "web" / "static" / "vendor"

MODERN_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

URL_RE = re.compile(r"""url\(\s*(['"]?)([^'")\s]+)\1\s*\)""")


def fetch_bytes(url: str, *, ua: str | None = None, attempts: int = 4) -> bytes:
    # Default to a real-browser UA — cdn.tailwindcss.com 403s the urllib default.
    # Retry transient TLS / network errors with exponential backoff.
    import time
    headers = {"User-Agent": ua or MODERN_UA}
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
            if i == attempts - 1:
                break
            wait = 1.5 * (2 ** i)
            print(f"    retry {i+1}/{attempts-1} after {wait:.1f}s ({exc})")
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def download(url: str, out_path: Path, *, ua: str | None = None) -> None:
    """Fetch ``url`` and write to ``out_path`` (creating parent dirs)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    data = fetch_bytes(url, ua=ua)
    out_path.write_bytes(data)
    print(f"  → {out_path.relative_to(REPO_ROOT)}  ({len(data):,} bytes)")


def mirror_css(
    css_url: str,
    out_css: Path,
    *,
    font_subdir: str = "",
    ua: str | None = None,
) -> None:
    """Fetch CSS at ``css_url``; mirror any ``url(...)`` references locally.

    Absolute URLs (http/https) are downloaded to
    ``out_css.parent / font_subdir / <basename>`` and the URL in the CSS is
    rewritten to ``<font_subdir>/<basename>`` (relative to the CSS file).
    Relative URLs are resolved against ``css_url``, downloaded keeping their
    relative path under ``out_css.parent``, and left unrewritten.
    """
    print(f"CSS: {css_url}")
    text = fetch_bytes(css_url, ua=ua).decode("utf-8")
    out_css.parent.mkdir(parents=True, exist_ok=True)
    font_dir = out_css.parent / font_subdir if font_subdir else out_css.parent

    rewrites: dict[str, str] = {}
    for m in URL_RE.finditer(text):
        ref = m.group(2)
        if ref.startswith("data:"):
            continue
        if ref.startswith(("http://", "https://")):
            basename = Path(urllib.parse.urlparse(ref).path).name
            local = font_dir / basename
            if not local.exists():
                download(ref, local, ua=ua)
            rel = f"{font_subdir}/{basename}" if font_subdir else basename
            rewrites[ref] = rel
        else:
            full = urllib.parse.urljoin(css_url, ref)
            rel_path = ref.lstrip("./").lstrip("/")
            local = out_css.parent / rel_path
            if not local.exists():
                download(full, local, ua=ua)
            # Relative URL — no rewrite, original path still resolves correctly.

    if rewrites:
        def sub(m: "re.Match[str]") -> str:
            ref = m.group(2)
            return f"url({rewrites[ref]})" if ref in rewrites else m.group(0)
        text = URL_RE.sub(sub, text)

    out_css.write_text(text, encoding="utf-8")
    print(f"  → {out_css.relative_to(REPO_ROOT)}  ({len(text):,} chars)")


def main() -> int:
    print(f"Vendor target: {VENDOR_DIR}\n")

    # 1. Tailwind Play CDN (single JIT-compiler JS file)
    download(
        "https://cdn.tailwindcss.com",
        VENDOR_DIR / "tailwind" / "tailwind-play.js",
    )

    # 2. Google Fonts — Outfit + Sora, woff2 only (requires a modern UA)
    mirror_css(
        "https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=Sora:wght@500;600;700&display=swap",
        VENDOR_DIR / "google-fonts" / "fonts.css",
        font_subdir="files",
        ua=MODERN_UA,
    )

    # 3. Twemoji country-flag polyfill — the package only ships the woff2, no
    # CSS file. The original base.html referenced a .css path that 404s on
    # jsDelivr (silently broken on Windows). Download the woff2 and write a
    # hand-crafted @font-face declaration.
    download(
        "https://cdn.jsdelivr.net/npm/country-flag-emoji-polyfill@0.1.8/dist/TwemojiCountryFlags.woff2",
        VENDOR_DIR / "twemoji-flags" / "TwemojiCountryFlags.woff2",
    )
    twemoji_css = (
        "@font-face {\n"
        "  font-family: 'Twemoji Country Flags';\n"
        "  unicode-range: U+1F1E6-1F1FF, U+1F3F4, U+E0062-E007F;\n"
        "  src: url('TwemojiCountryFlags.woff2') format('woff2');\n"
        "}\n"
    )
    (VENDOR_DIR / "twemoji-flags" / "TwemojiCountryFlags.css").write_text(twemoji_css, encoding="utf-8")
    print(f"  → web/static/vendor/twemoji-flags/TwemojiCountryFlags.css  ({len(twemoji_css)} chars, hand-written)")

    # 4. KaTeX 0.16.11 — CSS + ~20 fonts (relative URLs), plus two JS files
    mirror_css(
        "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css",
        VENDOR_DIR / "katex" / "katex.min.css",
    )
    download(
        "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js",
        VENDOR_DIR / "katex" / "katex.min.js",
    )
    download(
        "https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js",
        VENDOR_DIR / "katex" / "contrib" / "auto-render.min.js",
    )

    # 5. pdfjs-dist 4.6.82 — main module + worker
    download(
        "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/build/pdf.mjs",
        VENDOR_DIR / "pdfjs" / "pdf.mjs",
    )
    download(
        "https://cdn.jsdelivr.net/npm/pdfjs-dist@4.6.82/build/pdf.worker.mjs",
        VENDOR_DIR / "pdfjs" / "pdf.worker.mjs",
    )

    # 6. Chart.js 4.4.6 (UMD min)
    download(
        "https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js",
        VENDOR_DIR / "chartjs" / "chart.umd.min.js",
    )

    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
