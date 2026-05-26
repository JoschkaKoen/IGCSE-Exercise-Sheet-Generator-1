"""Per-paper exam-question extractor for the Learn-page content pipeline.

Reuses the xscore scaffold chain (via ``eXam.xscore_adapter.load_scaffold_api``)
to extract structured question YAML from empty Cambridge exam-paper PDFs. Same
sequence as ``eXam/bank.py``'s :func:`ensure_paper_indexed`, minus the
mark-scheme branch and the per-question PDF snippet renderer — we only need
the question text + structure for downstream subtopic matching.

Two modes (CLI flag ``--test-mode``):

- **Phase 0 / test mode** — extract ``--samples-per-family`` papers (default 2)
  per (subject, variant-family) where ``family = variant // 10``. Output to
  ``output/learn/test_extractions/<subject>/<paper_stem>.yaml`` plus a
  ``_review.md`` index per subject linking PDF ↔ YAML for visual review.
  Disposable. Use this to verify extraction quality before the mass run.

- **Phase 1 / mass mode** — extract up to ``--max-papers`` (default 30) most
  recent papers per subject to ``syllabi/questions/<subject>/<paper_stem>.yaml``.
  Idempotent (skips existing unless ``--force``).

IGCSE physics/chemistry/biology are filtered to paper variants 2x/4x/6x;
all other subjects use every available variant. Both Cambridge filename
conventions are handled — long-form (``9702 Physics June 2025 Question Paper
22.pdf``) and short-code (``0625_w24_qp_63.pdf``). Output filenames use the
short-code form as the canonical stem regardless of source.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from eXercise.config import EXAM_ROOT_BY_KEY, PROJECT_ROOT, SYLLABI_DIR

QUESTIONS_DIR = SYLLABI_DIR / "questions"
TEST_EXTRACTIONS_DIR = PROJECT_ROOT / "output" / "learn" / "test_extractions"
EXTRACTION_ERRORS_LOG = PROJECT_ROOT / "output" / "learn" / "extraction_errors.log"

# IGCSE phys/chem/bio: only paper 2x/4x/6x (extended theory + alt-to-practical).
# A-level + IGCSE math/CS: all variants.
_IGCSE_VARIANT_FILTER_SUBJECTS = {"physics", "chemistry", "biology"}
_ALLOWED_VARIANT_FAMILIES = {2, 4, 6}

CS_SUBJECTS = {"computer_science", "a_level_computer_science"}

# Long-form: "<code> <Subject words> <Month> <Year> Question [Pp]aper  <NN>.pdf"
# - Internal whitespace may be single or double space.
# - "Question Paper" / "Question paper" (capital P inconsistent across years).
# - Month can be "March", "June", "May/June", "November", "October/November".
_LONG_FORM_RE = re.compile(
    r"^(?P<code>\d{4})\s+.+?\s+"
    r"(?P<month>March|May/June|June|October/November|November)\s+"
    r"(?P<year>20\d{2})\s+"
    r"Question\s+[Pp]aper\s+"
    r"(?P<variant>\d{1,2})\.pdf$"
)

# Short-code: "<code>_<season><yy>_qp_<NN>.pdf"
_SHORT_CODE_RE = re.compile(
    r"^(?P<code>\d{4})_(?P<season>[msw])(?P<yy>\d{2})_qp_(?P<variant>\d{1,2})\.pdf$"
)

# Map season letters and month strings to a numeric month for sorting.
_SEASON_MONTH = {"m": 3, "s": 6, "w": 11}
_MONTH_INT = {
    "March": 3,
    "June": 6,
    "May/June": 6,
    "November": 11,
    "October/November": 11,
}
_MONTH_SEASON = {3: "m", 6: "s", 11: "w"}

# Reject files under this size as likely covers / corrupt placeholders.
_MIN_PDF_BYTES = 40 * 1024


@dataclass(frozen=True)
class PaperRef:
    """One question-paper PDF, with parsed metadata."""

    subject: str
    path: Path
    code: str
    year: int
    month: int  # 3, 6, or 11
    variant: int
    stem: str  # canonical short-code stem, used for output filename

    @property
    def family(self) -> int:
        """Variant family (``variant // 10``) — 1, 2, 3, 4, 5, or 6."""
        return self.variant // 10


def _canonical_stem(code: str, year: int, month: int, variant: int) -> str:
    season = _MONTH_SEASON[month]
    return f"{code}_{season}{year % 100:02d}_qp_{variant:02d}"


def _parse_long_form(name: str) -> tuple[str, int, int, int] | None:
    m = _LONG_FORM_RE.match(name)
    if not m:
        return None
    return (
        m.group("code"),
        int(m.group("year")),
        _MONTH_INT[m.group("month")],
        int(m.group("variant")),
    )


def _parse_short_code(name: str) -> tuple[str, int, int, int] | None:
    m = _SHORT_CODE_RE.match(name)
    if not m:
        return None
    yy = int(m.group("yy"))
    # 2-digit year → assume 2000-2099 window (Cambridge papers are post-2000).
    return (
        m.group("code"),
        2000 + yy,
        _SEASON_MONTH[m.group("season")],
        int(m.group("variant")),
    )


def _discover_papers(subject: str) -> list[PaperRef]:
    """Walk ``exams/<subject>/`` and return all question-paper PDFs.

    Tries both naming conventions; deduplicates by ``(code, year, month,
    variant)``. Filters out files under 40 KB. Does NOT apply variant or
    max-papers limits — caller decides.
    """
    root = EXAM_ROOT_BY_KEY.get(subject)
    if not root or not root.exists():
        return []

    by_key: dict[tuple[str, int, int, int], PaperRef] = {}
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.suffix.lower() != ".pdf":
            continue
        parsed = _parse_long_form(path.name) or _parse_short_code(path.name)
        if parsed is None:
            continue
        try:
            if path.stat().st_size < _MIN_PDF_BYTES:
                continue
        except OSError:
            continue
        code, year, month, variant = parsed
        stem = _canonical_stem(code, year, month, variant)
        ref = PaperRef(
            subject=subject, path=path, code=code,
            year=year, month=month, variant=variant, stem=stem,
        )
        # If both naming conventions present for the same paper, the first
        # encountered (sorted iterdir → long-form usually beats short-code
        # alphabetically) wins. Either path works for extraction.
        by_key.setdefault((code, year, month, variant), ref)

    return list(by_key.values())


def _select_for_mass(papers: list[PaperRef], subject: str, max_papers: int) -> list[PaperRef]:
    """Apply variant filter (IGCSE phys/chem/bio) and take most-recent N."""
    if subject in _IGCSE_VARIANT_FILTER_SUBJECTS:
        papers = [p for p in papers if p.family in _ALLOWED_VARIANT_FAMILIES]
    papers = sorted(papers, key=lambda p: (-p.year, -p.month, p.variant))
    return papers[:max_papers]


def _select_for_test(
    papers: list[PaperRef], subject: str, samples_per_family: int,
) -> list[PaperRef]:
    """Sample N most-recent papers per (subject, variant-family)."""
    if subject in _IGCSE_VARIANT_FILTER_SUBJECTS:
        papers = [p for p in papers if p.family in _ALLOWED_VARIANT_FAMILIES]
    by_family: dict[int, list[PaperRef]] = {}
    for p in sorted(papers, key=lambda p: (-p.year, -p.month, p.variant)):
        by_family.setdefault(p.family, []).append(p)
    selected: list[PaperRef] = []
    for fam in sorted(by_family):
        selected.extend(by_family[fam][:samples_per_family])
    return selected


def _output_path(subject: str, stem: str, test_mode: bool) -> Path:
    base = TEST_EXTRACTIONS_DIR if test_mode else QUESTIONS_DIR
    return base / subject / f"{stem}.yaml"


def _log_error(subject: str, stem: str, exc: BaseException) -> None:
    EXTRACTION_ERRORS_LOG.parent.mkdir(parents=True, exist_ok=True)
    with EXTRACTION_ERRORS_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"--- {subject}/{stem} ---\n")
        fh.write(f"{type(exc).__name__}: {exc}\n")
        fh.write(traceback.format_exc())
        fh.write("\n")


def _extract_one(client: Any, fmt: Any, paper: PaperRef, out_path: Path) -> bool:
    """Run the xscore QP chain on one paper. Returns True on success.

    Mirrors ``eXam/bank.py:ensure_paper_indexed`` lines 121–153, minus the
    MS chain and snippet renderer. Writes the final ``{"questions": [...]}``
    YAML atomically.
    """
    from eXam.xscore_adapter import load_scaffold_api
    xs = load_scaffold_api()

    is_cs = paper.subject in CS_SUBJECTS

    with tempfile.TemporaryDirectory(prefix=f"exam_q_{paper.stem}_") as tmp:
        artifact_dir = Path(tmp)

        layout_result, layout_elapsed, layout_model = xs.detect_layout_phase(
            client, paper.path, artifact_dir,
        )
        actual_pdf, split_temp, _n_phys, n_split = xs.cut_exam_pdf_phase(
            paper.path, layout_result, artifact_dir,
            layout_model=layout_model, layout_elapsed=layout_elapsed,
        )
        try:
            detect_model, detect_thinking, detect_max = (
                xs.extract_question_numbers_model_config()
            )
            scaffold_nodes, _raw_layout = xs.extract_exam_question_numbers(
                client, detect_model, detect_thinking, detect_max,
                actual_exam_pdf=actual_pdf,
                layout_result=layout_result,
                split_pdf_path=split_temp,
                n_split_pages=n_split,
                artifact_dir=artifact_dir,
                fmt=fmt, is_cs=is_cs, should_cache=False,
            )
            fill_model, fill_thinking, fill_max = xs.extract_questions_model_config()
            raw_questions = xs.extract_exam_questions(
                client, fill_model, fill_thinking, fill_max,
                actual_exam_pdf=actual_pdf,
                scaffold_nodes=scaffold_nodes,
                artifact_dir=artifact_dir,
                fmt=fmt, is_cs=is_cs, should_cache=False,
            )
        finally:
            if split_temp:
                try:
                    split_temp.unlink(missing_ok=True)
                except OSError:
                    pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_out = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_out.write_text(
        yaml.safe_dump({"questions": raw_questions}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp_out.replace(out_path)
    return True


def _write_review_md(subject: str, papers: list[PaperRef], subj_out_dir: Path) -> None:
    """Write ``_review.md`` linking each extracted YAML to its source PDF."""
    lines = [f"# Test extractions — {subject}\n"]
    for p in papers:
        out_yaml = subj_out_dir / f"{p.stem}.yaml"
        if not out_yaml.exists():
            continue
        rel_pdf = Path("../../../..") / "exams" / subject / p.path.name
        # Spaces in PDF names need URL-style encoding for markdown link parsers,
        # but most editors accept literal spaces in inline links — keep readable.
        lines.append(
            f"- `{p.stem}` ({p.year}-{p.month:02d}, variant {p.variant})"
            f" → [PDF]({rel_pdf}) · [YAML]({p.stem}.yaml)"
        )
    review_path = subj_out_dir / "_review.md"
    review_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_subject(
    subject: str, *, test_mode: bool, max_papers: int, samples_per_family: int,
    force: bool, dry_run: bool, workers: int,
) -> tuple[int, int, int]:
    """Returns (attempted, succeeded, skipped_already_done)."""
    all_papers = _discover_papers(subject)
    if not all_papers:
        print(f"{subject}: no question-paper PDFs found in exams/{subject}/", file=sys.stderr)
        return (0, 0, 0)

    if test_mode:
        selected = _select_for_test(all_papers, subject, samples_per_family)
    else:
        selected = _select_for_mass(all_papers, subject, max_papers)

    todo: list[PaperRef] = []
    skipped = 0
    for p in selected:
        out_path = _output_path(subject, p.stem, test_mode)
        if out_path.exists() and not force:
            skipped += 1
            continue
        todo.append(p)

    if dry_run:
        mode_label = "test" if test_mode else "mass"
        print(
            f"{subject} [{mode_label}]: {len(selected)} selected, "
            f"{skipped} already done, {len(todo)} to extract:",
            file=sys.stderr,
        )
        for p in todo:
            print(f"  - {p.stem}  ({p.path.name})", file=sys.stderr)
        return (len(todo), 0, skipped)

    if not todo:
        print(
            f"{subject}: all {len(selected)} papers already extracted "
            f"(use --force to re-extract)",
            file=sys.stderr,
        )
        if test_mode:
            # Still refresh _review.md in case files moved.
            _write_review_md(subject, selected, _output_path(subject, "_", True).parent)
        return (0, 0, skipped)

    # Lazy: deferred until we know we're extracting (heavy imports).
    from eXercise.ai_client import make_gemini_native_client
    from eXam.xscore_adapter import load_scaffold_api

    client = make_gemini_native_client()
    if client is None:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) not set — required for question extraction."
        )
    fmt = load_scaffold_api().get_scaffold_format()

    print(
        f"{subject}: extracting {len(todo)} paper(s) "
        f"({skipped} skipped, {workers} workers) …",
        file=sys.stderr,
    )

    succeeded = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _extract_one, client, fmt, p,
                _output_path(subject, p.stem, test_mode),
            ): p
            for p in todo
        }
        done_count = 0
        for fut in as_completed(futures):
            p = futures[fut]
            done_count += 1
            try:
                fut.result()
                succeeded += 1
                print(
                    f"  [{done_count}/{len(todo)}] ok: {p.stem}",
                    file=sys.stderr,
                )
            except Exception as exc:  # noqa: BLE001
                _log_error(subject, p.stem, exc)
                print(
                    f"  [{done_count}/{len(todo)}] FAIL {p.stem}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )

    if test_mode:
        _write_review_md(subject, selected, _output_path(subject, "_", True).parent)

    return (len(todo), succeeded, skipped)


# ── Loader (used by Phase 2 / matcher and any downstream consumer) ──────────
def load_questions_for_subject(subject: str) -> list[dict]:
    """Yield every extracted question for *subject*, with ``paper_stem`` set.

    Reads all ``syllabi/questions/<subject>/<stem>.yaml`` files (skipping
    leading-underscore index files like ``_by_subtopic.yaml``).
    """
    subj_dir = QUESTIONS_DIR / subject
    if not subj_dir.exists():
        return []
    out: list[dict] = []
    for yaml_path in sorted(subj_dir.glob("*.yaml")):
        if yaml_path.name.startswith("_"):
            continue
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        stem = yaml_path.stem
        for q in data.get("questions") or []:
            if isinstance(q, dict):
                out.append({**q, "paper_stem": stem})
    return out


def main() -> int:
    from eXercise.env_load import load_project_env
    load_project_env()

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", help="Only process this subject_key (e.g. physics)")
    parser.add_argument(
        "--max-papers", type=int, default=30,
        help="Mass mode: max papers per subject (default 30)",
    )
    parser.add_argument(
        "--test-mode", action="store_true",
        help="Phase 0: sample papers per (subject, variant-family) for visual review",
    )
    parser.add_argument(
        "--samples-per-family", type=int, default=2,
        help="Test mode: papers per family (default 2)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-extract papers whose output already exists",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List what would be extracted, don't call the API",
    )
    parser.add_argument(
        "--workers", type=int,
        default=int(__import__("os").environ.get("EXAM_EXTRACT_WORKERS", "4") or "4"),
        help="ThreadPoolExecutor workers (default 4 or $EXAM_EXTRACT_WORKERS)",
    )
    args = parser.parse_args()

    if args.subject and args.subject not in EXAM_ROOT_BY_KEY:
        print(f"Unknown subject_key: {args.subject!r}", file=sys.stderr)
        print(f"Available: {', '.join(EXAM_ROOT_BY_KEY)}", file=sys.stderr)
        return 2

    subjects = [args.subject] if args.subject else list(EXAM_ROOT_BY_KEY.keys())

    total_attempted = 0
    total_ok = 0
    total_skipped = 0
    for subj in subjects:
        attempted, ok, skipped = _run_subject(
            subj,
            test_mode=args.test_mode,
            max_papers=args.max_papers,
            samples_per_family=args.samples_per_family,
            force=args.force,
            dry_run=args.dry_run,
            workers=args.workers,
        )
        total_attempted += attempted
        total_ok += ok
        total_skipped += skipped

    mode_label = "test" if args.test_mode else "mass"
    print(
        f"\n[{mode_label}] done: {total_ok}/{total_attempted} extracted, "
        f"{total_skipped} already present.",
        file=sys.stderr,
    )
    if args.test_mode and total_ok > 0:
        print(
            f"  Review extracted YAML in: {TEST_EXTRACTIONS_DIR}",
            file=sys.stderr,
        )
        print(
            f"  Each subject has a _review.md with PDF/YAML links.",
            file=sys.stderr,
        )
    if not args.dry_run and total_attempted > total_ok:
        print(
            f"  See {EXTRACTION_ERRORS_LOG} for failure traces.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
