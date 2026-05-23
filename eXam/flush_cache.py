"""Flush cached helper files (and optionally snippet PDFs) under the eXam bank.

Walks ``output/eXam/bank/`` and deletes every ``helpers/`` directory (so all
four `.md` files per question across every subject and every paper). With
``--snippets``, also deletes every per-question ``question.pdf`` so the next
view re-renders them through ``eXam.bank.ensure_question_pdf`` — use this when
``layout_vector_strips_to_pdf``'s snippet output format has changed. The bank
itself (paper YAMLs) is left intact — re-enrichment is expensive and rarely
needed. If you really want a full reset: ``rm -rf output/eXam/bank/``.

Usage:
    .venv/bin/python -m eXam.flush_cache              # helpers only
    .venv/bin/python -m eXam.flush_cache --snippets   # helpers + snippet PDFs
"""

from __future__ import annotations

import argparse
import shutil
import sys

from eXam.bank import BANK_ROOT


def main() -> int:
    parser = argparse.ArgumentParser(prog="eXam.flush_cache")
    parser.add_argument(
        "--snippets",
        action="store_true",
        help="also delete every per-question question.pdf (next view re-renders).",
    )
    args = parser.parse_args()

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

    if args.snippets:
        removed_snippets = 0
        for pdf in BANK_ROOT.rglob("question.pdf"):
            try:
                pdf.unlink()
                removed_snippets += 1
            except OSError as e:
                print(f"[flush] could not remove {pdf}: {e}")
        print(f"[flush] removed {removed_snippets} snippet PDF(s) under {BANK_ROOT}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
