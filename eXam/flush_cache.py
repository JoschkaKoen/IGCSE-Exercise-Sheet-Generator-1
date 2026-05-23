"""Flush all cached helper files under the eXam bank.

Walks ``output/eXam/bank/`` and deletes every ``helpers/`` directory (so all
four `.md` files per question across every subject and every paper). The bank
itself (paper YAMLs, ``question.pdf``) is left intact — re-enrichment is
expensive and rarely needed. If you really want a full reset:
``rm -rf output/eXam/bank/``.

Usage:
    .venv/bin/python -m eXam.flush_cache
"""

from __future__ import annotations

import shutil
import sys

from eXam.bank import BANK_ROOT


def main() -> int:
    if not BANK_ROOT.is_dir():
        print(f"[flush] bank dir not found: {BANK_ROOT}")
        return 0
    removed_dirs = 0
    removed_files = 0
    for helpers_dir in BANK_ROOT.rglob("helpers"):
        if not helpers_dir.is_dir():
            continue
        files = list(helpers_dir.glob("*.md"))
        removed_files += len(files)
        shutil.rmtree(helpers_dir)
        removed_dirs += 1
    print(
        f"[flush] removed {removed_files} helper file(s) "
        f"across {removed_dirs} question dir(s) under {BANK_ROOT}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
