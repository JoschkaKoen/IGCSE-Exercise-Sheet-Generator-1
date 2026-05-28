#!/usr/bin/env python3
"""Regression test: generate one exercise sheet per paper component per subject.

Drives eXercise.py via subprocess once per row in MATRIX. Captures stdout/stderr,
parses the printed "Output directory:" line to learn where the generator wrote
its run folder, then symlinks all run folders into a single deliverable directory.

Usage:
    .venv/bin/python scripts/regression_test_all_subjects.py            # all 46 rows
    .venv/bin/python scripts/regression_test_all_subjects.py --subject igcse_physics
    .venv/bin/python scripts/regression_test_all_subjects.py --row-ids igcse_physics_p21_w25,igcse_physics_p41_w25

The manifest is written incrementally so killing and restarting the script
resumes from where it left off (rows already marked status=="ok" are skipped).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"
EXERCISE_PY = REPO_ROOT / "eXercise.py"
DELIVERABLE = REPO_ROOT / "output" / "exercise_tests_2026-05-28"

# MCQ-model override (test-run-only) — flash-lite is cheaper and explanations
# don't need deep reasoning. thinking=0, max_tokens=16384 preserves output budget.
MCQ_MODEL_OVERRIDE = "gemini-3.1-flash-lite, 0, 16384"

PER_ROW_TIMEOUT_S = 900  # MCQ explanation passes occasionally need ~10 min on large papers

TRANSIENT_ERROR_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"rate.?limit", r"503", r"504", r"timeout", r"timed out",
        r"connection reset", r"connection refused", r"temporarily unavailable",
    )
]


@dataclass
class Row:
    row_id: str            # e.g. "igcse_physics_p21_w25"
    subject_key: str       # e.g. "igcse_physics" (matches EXAM_ROOT_BY_KEY)
    level_label: str       # "IGCSE" or "A-Level"
    subject_label: str     # "Physics", "Chemistry", ...
    session_label: str     # "November 2025", "June 2025"
    paper_number: int      # 21, 41, 61, ...
    qp_path: Path          # absolute path to the question paper PDF (preflight check)
    ms_path: Path          # absolute path to the mark scheme PDF (preflight check)

    @property
    def prompt(self) -> str:
        return (
            f"{self.level_label} {self.subject_label} {self.session_label} "
            f"paper {self.paper_number}, all questions, include mark scheme"
        )


def _exam_dir(level: str, slug: str) -> Path:
    return REPO_ROOT / "exams" / level / slug


def build_matrix() -> list[Row]:
    """The full 46-row regression matrix per the approved plan."""
    rows: list[Row] = []

    # IGCSE sciences — Extended-tier only (papers 2, 4, 6)
    for subject_key, subject_label, slug, code, session, papers in [
        ("igcse_physics", "Physics", "physics_0625", "0625", ("November 2025", "w25"), [21, 41, 61]),
        ("igcse_chemistry", "Chemistry", "chemistry_0620", "0620", ("June 2025", "s25"), [21, 41, 61]),
        ("igcse_biology", "Biology", "biology_0610", "0610", ("June 2025", "s25"), [21, 41, 61]),
    ]:
        ed = _exam_dir("igcse", slug)
        sess_label, sess_slug = session
        for p in papers:
            rows.append(Row(
                row_id=f"{subject_key}_p{p}_{sess_slug}",
                subject_key=subject_key,
                level_label="IGCSE",
                subject_label=subject_label,
                session_label=sess_label,
                paper_number=p,
                qp_path=ed / f"{code} {subject_label} {sess_label} Question Paper  {p}.pdf",
                ms_path=ed / f"{code} {subject_label} {sess_label} Mark Scheme  {p}.pdf",
            ))

    # IGCSE Mathematics — all four papers
    ed = _exam_dir("igcse", "mathematics_0580")
    for p in [11, 21, 31, 41]:
        rows.append(Row(
            row_id=f"igcse_mathematics_p{p}_s25",
            subject_key="igcse_mathematics",
            level_label="IGCSE", subject_label="Mathematics",
            session_label="June 2025", paper_number=p,
            qp_path=ed / f"0580 Mathematics June 2025 Question Paper  {p}.pdf",
            ms_path=ed / f"0580 Mathematics June 2025 Mark Scheme  {p}.pdf",
        ))

    # IGCSE Computer Science — Nov 2025 (no June 2025)
    ed = _exam_dir("igcse", "computer_science_0478")
    for p in [11, 21]:
        rows.append(Row(
            row_id=f"igcse_computer_science_p{p}_w25",
            subject_key="igcse_computer_science",
            level_label="IGCSE", subject_label="Computer Science",
            session_label="November 2025", paper_number=p,
            qp_path=ed / f"0478 Computer Science November 2025 Question Paper  {p}.pdf",
            ms_path=ed / f"0478 Computer Science November 2025 Mark Scheme  {p}.pdf",
        ))

    # IGCSE Business Studies / Economics — June 2025
    for subject_key, subject_label, slug, code in [
        ("igcse_business_studies", "Business Studies", "business_studies_0450", "0450"),
        ("igcse_economics", "Economics", "economics_0455", "0455"),
    ]:
        ed = _exam_dir("igcse", slug)
        for p in [11, 21]:
            rows.append(Row(
                row_id=f"{subject_key}_p{p}_s25",
                subject_key=subject_key,
                level_label="IGCSE", subject_label=subject_label,
                session_label="June 2025", paper_number=p,
                qp_path=ed / f"{code} {subject_label} June 2025 Question Paper  {p}.pdf",
                ms_path=ed / f"{code} {subject_label} June 2025 Mark Scheme  {p}.pdf",
            ))

    # A-Level sciences — all 5 components, June 2025
    for subject_key, subject_label, slug, code in [
        ("a_level_physics", "Physics", "physics_9702", "9702"),
        ("a_level_chemistry", "Chemistry", "chemistry_9701", "9701"),
        ("a_level_biology", "Biology", "biology_9700", "9700"),
    ]:
        ed = _exam_dir("a_level", slug)
        for p in [11, 21, 31, 41, 51]:
            rows.append(Row(
                row_id=f"{subject_key}_p{p}_s25",
                subject_key=subject_key,
                level_label="A-Level", subject_label=subject_label,
                session_label="June 2025", paper_number=p,
                qp_path=ed / f"{code} {subject_label} June 2025 Question Paper  {p}.pdf",
                ms_path=ed / f"{code} {subject_label} June 2025 Mark Scheme  {p}.pdf",
            ))

    # A-Level CS, Business, Economics — 4 components, June 2025
    for subject_key, subject_label, slug, code in [
        ("a_level_computer_science", "Computer Science", "computer_science_9618", "9618"),
        ("a_level_business", "Business", "business_9609", "9609"),
        ("a_level_economics", "Economics", "economics_9708", "9708"),
    ]:
        ed = _exam_dir("a_level", slug)
        for p in [11, 21, 31, 41]:
            rows.append(Row(
                row_id=f"{subject_key}_p{p}_s25",
                subject_key=subject_key,
                level_label="A-Level", subject_label=subject_label,
                session_label="June 2025", paper_number=p,
                qp_path=ed / f"{code} {subject_label} June 2025 Question Paper  {p}.pdf",
                ms_path=ed / f"{code} {subject_label} June 2025 Mark Scheme  {p}.pdf",
            ))

    assert len(rows) == 46, f"matrix size mismatch: {len(rows)} != 46"
    return rows


def load_manifest(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return []


def save_manifest(path: Path, manifest: list[dict]) -> None:
    path.write_text(json.dumps(manifest, indent=2))


def parse_output_dir(stdout: str) -> Path | None:
    """eXercise prints 'Output directory: <abs path>' once when the run folder is created."""
    for line in stdout.splitlines():
        if line.startswith("Output directory: "):
            return Path(line[len("Output directory: "):].strip())
    return None


def looks_transient(text: str) -> bool:
    return any(p.search(text) for p in TRANSIENT_ERROR_PATTERNS)


def run_row(row: Row, log_path: Path, attempt: int = 1) -> dict:
    """Run one row as a subprocess. Returns a manifest entry."""
    if not row.qp_path.exists():
        return _entry(row, status="skip", error=f"QP missing: {row.qp_path}")
    if not row.ms_path.exists():
        # MS missing is unusual but not fatal — the NL resolver will skip MS attachment.
        # Record as a warning in the entry but still attempt.
        pass

    env = os.environ.copy()
    env["MCQ_MODEL"] = MCQ_MODEL_OVERRIDE

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [str(VENV_PY), str(EXERCISE_PY), row.prompt],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=PER_ROW_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        duration = time.monotonic() - t0
        log_path.write_text(f"TIMEOUT after {PER_ROW_TIMEOUT_S}s\n\nstdout:\n{e.stdout or ''}\n\nstderr:\n{e.stderr or ''}")
        return _entry(row, status="fail", error=f"timeout after {PER_ROW_TIMEOUT_S}s", duration_s=duration, log_path=str(log_path.relative_to(DELIVERABLE)))

    duration = time.monotonic() - t0
    log_path.write_text(
        f"prompt: {row.prompt}\nattempt: {attempt}\nexit: {proc.returncode}\nduration_s: {duration:.1f}\n\n"
        f"=== stdout ===\n{proc.stdout}\n\n=== stderr ===\n{proc.stderr}\n"
    )

    if proc.returncode != 0:
        # Retry once on transient errors
        if attempt == 1 and looks_transient(proc.stderr + proc.stdout):
            print(f"  [retry] transient error detected; retrying once …", flush=True)
            return run_row(row, log_path, attempt=2)
        return _entry(row, status="fail", error=f"exit={proc.returncode} (see log)", duration_s=duration, log_path=str(log_path.relative_to(DELIVERABLE)))

    run_folder = parse_output_dir(proc.stdout)
    if run_folder is None or not run_folder.exists():
        return _entry(row, status="fail", error="run folder not found in stdout", duration_s=duration, log_path=str(log_path.relative_to(DELIVERABLE)))

    # Find exercise + answers PDFs in the run folder
    pdfs = sorted(run_folder.glob("*.pdf"))
    ex_pdf = next((p for p in pdfs if "_answers" not in p.stem and not p.stem.endswith("_2up") and not p.stem.endswith("_4up") and not p.stem.endswith("_ranking")), None)
    ans_pdf = next((p for p in pdfs if p.stem.endswith("_answers")), None)

    ex_pages = _pdf_pages(ex_pdf) if ex_pdf else 0
    ans_pages = _pdf_pages(ans_pdf) if ans_pdf else 0

    return _entry(
        row,
        status="ok" if ex_pdf and ex_pdf.stat().st_size > 0 else "fail",
        run_folder=str(run_folder),
        exercise_pdf=str(ex_pdf) if ex_pdf else "",
        answers_pdf=str(ans_pdf) if ans_pdf else "",
        exercise_pages=ex_pages,
        answers_pages=ans_pages,
        duration_s=duration,
        log_path=str(log_path.relative_to(DELIVERABLE)),
    )


def _pdf_pages(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    try:
        import fitz  # pymupdf
        with fitz.open(str(path)) as doc:
            return doc.page_count
    except Exception:
        return 0


def _entry(row: Row, *, status: str, error: str | None = None,
           run_folder: str = "", exercise_pdf: str = "", answers_pdf: str = "",
           exercise_pages: int = 0, answers_pages: int = 0,
           duration_s: float = 0.0, log_path: str = "") -> dict:
    return {
        "row_id": row.row_id,
        "subject_key": row.subject_key,
        "paper_number": row.paper_number,
        "session": row.session_label,
        "prompt": row.prompt,
        "status": status,
        "error": error,
        "run_folder": run_folder,
        "exercise_pdf": exercise_pdf,
        "answers_pdf": answers_pdf,
        "exercise_pages": exercise_pages,
        "answers_pages": answers_pages,
        "duration_s": round(duration_s, 1),
        "log_path": log_path,
    }


def symlink_sheets(manifest: list[dict]) -> None:
    """Symlink each ok run_folder into sheets/<row_id>/ for browsability."""
    sheets_dir = DELIVERABLE / "sheets"
    sheets_dir.mkdir(parents=True, exist_ok=True)
    for entry in manifest:
        if entry["status"] != "ok" or not entry["run_folder"]:
            continue
        link = sheets_dir / entry["row_id"]
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(entry["run_folder"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--subject", help="restrict to one subject_key (e.g. igcse_physics)")
    ap.add_argument("--row-ids", help="comma-separated list of row_ids to run")
    ap.add_argument("--force", action="store_true", help="ignore manifest and re-run all rows")
    args = ap.parse_args()

    if not VENV_PY.exists():
        sys.exit(f"venv python missing: {VENV_PY}")
    if not EXERCISE_PY.exists():
        sys.exit(f"eXercise.py missing: {EXERCISE_PY}")

    matrix = build_matrix()
    if args.subject:
        matrix = [r for r in matrix if r.subject_key == args.subject]
    if args.row_ids:
        wanted = set(args.row_ids.split(","))
        matrix = [r for r in matrix if r.row_id in wanted]
    if not matrix:
        sys.exit("matrix is empty after filters")

    DELIVERABLE.mkdir(parents=True, exist_ok=True)
    (DELIVERABLE / "logs").mkdir(exist_ok=True)
    manifest_path = DELIVERABLE / "manifest.json"

    manifest = [] if args.force else load_manifest(manifest_path)
    done_ok = {e["row_id"] for e in manifest if e["status"] == "ok"}

    print(f"Running {len(matrix)} rows (already ok: {len(done_ok)})")
    for i, row in enumerate(matrix, 1):
        if row.row_id in done_ok:
            print(f"[{i}/{len(matrix)}] {row.row_id} — already ok, skipping")
            continue
        print(f"[{i}/{len(matrix)}] {row.row_id} — {row.prompt}", flush=True)

        log_path = DELIVERABLE / "logs" / f"{row.row_id}.log"
        entry = run_row(row, log_path)

        # Replace existing entry for this row (if any) or append
        manifest = [e for e in manifest if e["row_id"] != row.row_id]
        manifest.append(entry)
        manifest.sort(key=lambda e: e["row_id"])
        save_manifest(manifest_path, manifest)

        status_msg = entry["status"].upper()
        if entry["status"] == "ok":
            print(f"  → {status_msg} ({entry['duration_s']:.1f}s, ex={entry['exercise_pages']}p, ans={entry['answers_pages']}p)")
        else:
            print(f"  → {status_msg}: {entry.get('error')}")

    symlink_sheets(manifest)
    print(f"\nDeliverable: {DELIVERABLE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
