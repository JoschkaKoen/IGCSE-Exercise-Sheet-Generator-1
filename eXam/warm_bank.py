"""Pre-warm the eXam bank: index every QP for one or all subjects for a year.

Each paper indexed costs several AI calls (xscore steps 14–22) but is then
cached forever. Helper pregeneration is intentionally NOT part of warming —
open-mode helpers stay lazy.

Usage:
    .venv/bin/python -m eXam.warm_bank --year 2025 --subject physics
    .venv/bin/python -m eXam.warm_bank --year 2025 --subject all
"""

from __future__ import annotations

import argparse
import sys
import time

from eXercise.config import EXAM_ROOT_BY_KEY
from eXercise.env_load import load_project_env


def _warm_subject(subject: str, year: int) -> tuple[int, int]:
    """Returns ``(ok, failed)`` for the subject."""
    from eXam.bank import ensure_paper_indexed
    from eXam.open_mode import list_practice_papers, pair_mark_scheme

    papers = list_practice_papers(subject, year)
    if not papers:
        print(f"[warm] {subject}: no {year} papers found, skipping")
        return 0, 0
    ok = 0
    failed = 0
    for i, qp in enumerate(papers, start=1):
        ms = pair_mark_scheme(qp)
        if ms is None:
            print(f"[warm] {subject} ({i}/{len(papers)}): {qp.name} — no MS, skipping")
            failed += 1
            continue
        print(f"[warm] {subject} ({i}/{len(papers)}): {qp.name}")
        t0 = time.monotonic()
        try:
            ensure_paper_indexed(qp, ms, subject)
            print(f"[warm]   done in {time.monotonic()-t0:.1f}s")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"[warm]   failed: {e}")
            failed += 1
    return ok, failed


def _cli() -> int:
    p = argparse.ArgumentParser(prog="eXam.warm_bank")
    p.add_argument("--year", type=int, default=2025)
    p.add_argument(
        "--subject",
        required=True,
        help="subject slug (e.g. physics) or 'all'",
    )
    args = p.parse_args()
    load_project_env()
    if args.subject == "all":
        subjects = list(EXAM_ROOT_BY_KEY.keys())
    elif args.subject in EXAM_ROOT_BY_KEY:
        subjects = [args.subject]
    else:
        print(
            f"error: unknown subject {args.subject!r}. "
            f"Valid: {', '.join(EXAM_ROOT_BY_KEY)} or 'all'",
            file=sys.stderr,
        )
        return 2
    total_ok = 0
    total_failed = 0
    for subj in subjects:
        ok, failed = _warm_subject(subj, args.year)
        total_ok += ok
        total_failed += failed
    print(f"\n[warm] done: {total_ok} papers indexed, {total_failed} skipped/failed")
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_cli())
