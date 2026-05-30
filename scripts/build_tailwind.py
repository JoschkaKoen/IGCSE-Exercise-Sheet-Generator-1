#!/usr/bin/env python3
"""Build the pre-compiled Tailwind stylesheet (replaces the runtime Play CDN JIT).

The web UI used to load Tailwind's Play CDN, which compiles CSS *in the browser*
on every page load — slow on large DOMs (the practice landing is ~230 KB) and
~400 KB of render-blocking JS. We now ship a pre-built stylesheet instead.

This script downloads the pinned standalone ``tailwindcss`` CLI (cached under a
gitignored ``.cache/`` dir — no Node/npm needed) and compiles
``web/tailwind.input.css`` → ``web/static/css/00-tailwind.css`` using
``web/tailwind.config.js``.

    python scripts/build_tailwind.py

⚠️  RECOMPILE + COMMIT the regenerated ``web/static/css/00-tailwind.css`` after
    adding/removing Tailwind utility classes in any template or JS file. The
    committed CSS is what ships (the Docker image does NOT rebuild it). Classes
    not present at build time will have no styles (unlike the old browser JIT).
"""

from __future__ import annotations

import platform
import stat
import subprocess
import sys
import urllib.request
from pathlib import Path

# Match the Tailwind major/minor the Play CDN used (v3 config API:
# `tailwind.config = {...}`). Bump deliberately + re-verify rendering.
TAILWIND_VERSION = "v3.4.17"

REPO = Path(__file__).resolve().parents[1]
CACHE_DIR = REPO / ".cache" / "tailwindcss"  # gitignored
CONFIG = REPO / "web" / "tailwind.config.js"
INPUT = REPO / "web" / "tailwind.input.css"
OUTPUT = REPO / "web" / "static" / "css" / "00-tailwind.css"


def _asset_name() -> str:
    system = platform.system().lower()
    arch = platform.machine().lower()
    arm = arch in ("arm64", "aarch64")
    if system == "darwin":
        return "tailwindcss-macos-arm64" if arm else "tailwindcss-macos-x64"
    if system == "linux":
        return "tailwindcss-linux-arm64" if arm else "tailwindcss-linux-x64"
    raise SystemExit(f"Unsupported platform: {system}/{arch}")


def _ensure_cli() -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    binary = CACHE_DIR / f"{_asset_name()}-{TAILWIND_VERSION}"
    if not binary.exists():
        url = (
            "https://github.com/tailwindlabs/tailwindcss/releases/download/"
            f"{TAILWIND_VERSION}/{_asset_name()}"
        )
        print(f"Downloading standalone tailwindcss CLI: {url}", file=sys.stderr)
        tmp = binary.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.chmod(tmp.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        tmp.replace(binary)
    return binary


def main() -> int:
    cli = _ensure_cli()
    cmd = [str(cli), "-c", str(CONFIG), "-i", str(INPUT), "-o", str(OUTPUT), "--minify"]
    print("Building:", " ".join(cmd), file=sys.stderr)
    # Run from web/ (the config dir) so the config's relative `content` globs
    # resolve consistently (standalone Tailwind resolves content from the config
    # location).
    subprocess.run(cmd, cwd=str(REPO / "web"), check=True)
    size_kb = OUTPUT.stat().st_size / 1024
    print(f"Wrote {OUTPUT.relative_to(REPO)} ({size_kb:.1f} KB)", file=sys.stderr)
    print("Remember to COMMIT the regenerated CSS.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
