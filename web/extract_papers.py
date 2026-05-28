"""Round-1 mass-extraction driver for exam questions.

Discovers question-paper PDFs under ``exams/<subject>/``, balances the
selection across paper-variant families, and indexes each paper into
``output/eXam/bank/<subject>/<paper_stem>/`` via
:func:`eXam.bank.ensure_paper_indexed` (the canonical empty-paper xscore
chain). Extraction is skipped for papers already indexed (the bank checks
``paper_sha.txt``).

Companion CLI: ``python -m web.extract_papers``.

Two CLIs in one module:

- ``--subject X --max-papers 30`` → discover, balance, extract; exit 0 when
  done. The agent then reviews using :func:`scan_subject` (run via the
  module's ``--scan`` sub-flag) and verifies suspicious items against
  source PDFs.
- ``--retry-paper "<bank_stem>"`` → delete that one paper's bank dir and
  re-extract it. The only way to re-do an existing paper. Used by the
  agent after the review step flags a FAIL.

Both Cambridge filename conventions are handled (long-form
``9618 Computer Science November 2025 Question Paper  31.pdf`` AND
short-code ``0478_w24_qp_22.pdf``).

The deterministic-scan rules live in :func:`scan_paper` / :func:`scan_subject`
and are intentionally NOT a separate quality module — the workflow is
"scan to surface candidates, agent verifies against PDF in-session."
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

import yaml

from eXercise.config import EXAM_ROOT_BY_KEY, PROJECT_ROOT

LEARN_OUTPUT_DIR = PROJECT_ROOT / "output" / "learn"
QUALITY_REVIEWS_DIR = LEARN_OUTPUT_DIR / "quality_reviews"
STATE_PATH = LEARN_OUTPUT_DIR / "extract_state.json"

# IGCSE phys/chem/bio: keep only paper variants 2x / 4x / 6x.
IGCSE_VARIANT_FILTER = {"igcse_physics", "igcse_chemistry", "igcse_biology"}
_ALLOWED_VARIANT_FAMILIES = {2, 4, 6}

# Reject files under this size as likely covers / corrupt placeholders.
_MIN_PDF_BYTES = 40 * 1024

# Long-form: "<code> <Subject words> <Month> <Year> Question [Pp]aper  <NN>.pdf"
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
_SEASON_MONTH = {"m": 3, "s": 6, "w": 11}
_MONTH_INT = {
    "March": 3, "June": 6, "May/June": 6,
    "November": 11, "October/November": 11,
}

# Scan thresholds (see plan, "Quality-review workflow / Step 1").
_TEXT_LEN_FAIL = 5000
_TEXT_LEN_WARN = 2000
_TOKEN_REPETITION_FAIL = 50
_LOW_ENTROPY_LEN_THRESHOLD = 1000
_LOW_ENTROPY_RATIO_FAIL = 0.005
_SHORT_TEXT_WARN = 10


class ExtractOutcome(str, Enum):
    SKIPPED = "skipped"   # bank already had a YAML
    EXTRACTED = "extracted"
    FAILED = "failed"     # ensure_paper_indexed raised


@dataclass(frozen=True)
class PaperRef:
    subject: str
    path: Path
    code: str
    year: int
    month: int        # 3, 6, or 11
    variant: int

    @property
    def family(self) -> int:
        return self.variant // 10

    @property
    def bank_stem(self) -> str:
        # The eXam.bank.bank_dir_for() uses the literal PDF stem (with spaces /
        # double-spaces). Match it exactly — do NOT synthesise a short-code.
        return self.path.stem


# ── Discovery ────────────────────────────────────────────────────────────


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
    return (
        m.group("code"),
        2000 + int(m.group("yy")),
        _SEASON_MONTH[m.group("season")],
        int(m.group("variant")),
    )


def discover(subject: str) -> list[PaperRef]:
    """Walk ``exams/<subject>/`` and return all question-paper PDFs."""
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
        ref = PaperRef(
            subject=subject, path=path, code=code,
            year=year, month=month, variant=variant,
        )
        by_key.setdefault((code, year, month, variant), ref)
    return list(by_key.values())


def select_for_mass(
    papers: list[PaperRef], subject: str, max_papers: int = 30,
) -> list[PaperRef]:
    """Round-robin pick across variant families, newest first within each.

    Guarantees every existing family is sampled before any family is
    over-sampled — e.g. IGCSE CS with 2 families → 15 each.
    """
    if subject in IGCSE_VARIANT_FILTER:
        papers = [p for p in papers if p.family in _ALLOWED_VARIANT_FAMILIES]
    by_fam: dict[int, list[PaperRef]] = defaultdict(list)
    for p in sorted(papers, key=lambda p: (-p.year, -p.month, p.variant)):
        by_fam[p.family].append(p)
    out: list[PaperRef] = []
    families = sorted(by_fam)
    while len(out) < max_papers and any(by_fam[f] for f in families):
        for f in families:
            if by_fam[f] and len(out) < max_papers:
                out.append(by_fam[f].pop(0))
    return out


# ── Extraction ───────────────────────────────────────────────────────────


def _bank_paths(paper: PaperRef) -> tuple[Path, Path]:
    """Return (bank_dir, exam_questions.yaml path) for a paper."""
    from eXam.bank import bank_dir_for
    bdir = bank_dir_for(paper.subject, paper.path)
    return bdir, bdir / "exam_questions.yaml"


def extract_one(paper: PaperRef) -> ExtractOutcome:
    """Skip if bank YAML exists; otherwise call ensure_paper_indexed."""
    bank_dir, yaml_path = _bank_paths(paper)
    if yaml_path.exists():
        return ExtractOutcome.SKIPPED
    # Lazy import: pulls google.genai + xscore transitively.
    from eXam.bank import ensure_paper_indexed
    try:
        ensure_paper_indexed(paper.path, ms_path=None, subject=paper.subject)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {paper.bank_stem}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return ExtractOutcome.FAILED
    return ExtractOutcome.EXTRACTED if yaml_path.exists() else ExtractOutcome.FAILED


def _wipe_bank_dir(bank_dir: Path) -> None:
    if bank_dir.exists():
        shutil.rmtree(bank_dir)


# ── Scan (deterministic quality checks) ──────────────────────────────────


@dataclass
class ScanIssue:
    qkey: str       # e.g. "1.1ai" or "36"
    is_leaf: bool
    rule: str       # text_length_fail, token_repetition_fail, …
    severity: str   # "warn" | "fail"
    detail: str


@dataclass
class PaperScan:
    subject: str
    bank_stem: str
    yaml_path: Path
    leaf_count: int
    total_text_chars: int
    issues: list[ScanIssue]

    @property
    def verdict(self) -> str:
        if any(i.severity == "fail" for i in self.issues):
            return "fail"
        if any(i.severity == "warn" for i in self.issues):
            return "warn"
        return "ok"


def _walk_questions(qs: list[dict], prefix: str = "") -> Iterator[tuple[str, dict, bool]]:
    """Yield (qkey, question_dict, is_leaf) for every node in the tree.

    ``qkey`` is the dotted path of question numbers — e.g. "1", "1.1a", "1.1a.1ai".
    """
    for q in qs:
        if not isinstance(q, dict):
            continue
        num = str(q.get("number") or "?")
        qkey = f"{prefix}.{num}" if prefix else num
        subs = q.get("subquestions") or []
        is_leaf = not subs
        yield qkey, q, is_leaf
        if subs:
            yield from _walk_questions(subs, qkey)


def _max_consecutive_token_run(text: str) -> tuple[str, int]:
    """Return (token, longest_consecutive_run) from whitespace-split text."""
    tokens = text.split()
    if not tokens:
        return ("", 0)
    max_token, max_run = tokens[0], 1
    cur_token, cur_run = tokens[0], 1
    for t in tokens[1:]:
        if t == cur_token:
            cur_run += 1
            if cur_run > max_run:
                max_token, max_run = cur_token, cur_run
        else:
            cur_token, cur_run = t, 1
    return (max_token, max_run)


def _check_node(qkey: str, q: dict, is_leaf: bool) -> list[ScanIssue]:
    issues: list[ScanIssue] = []
    text = str(q.get("text") or "")
    marks_raw = q.get("marks")
    marks = marks_raw if isinstance(marks_raw, int) else 0

    n = len(text)
    if n > _TEXT_LEN_FAIL:
        issues.append(ScanIssue(qkey, is_leaf, "text_length_fail", "fail",
                                f"{n} chars (limit {_TEXT_LEN_FAIL})"))
    elif n > _TEXT_LEN_WARN:
        issues.append(ScanIssue(qkey, is_leaf, "text_length_warn", "warn",
                                f"{n} chars (warn at {_TEXT_LEN_WARN})"))

    tok, run = _max_consecutive_token_run(text)
    if run > _TOKEN_REPETITION_FAIL:
        issues.append(ScanIssue(qkey, is_leaf, "token_repetition_fail", "fail",
                                f"token {tok!r} repeats {run}× consecutively"))

    if n > _LOW_ENTROPY_LEN_THRESHOLD:
        ratio = len(set(text)) / n
        if ratio < _LOW_ENTROPY_RATIO_FAIL:
            issues.append(ScanIssue(qkey, is_leaf, "low_entropy_fail", "fail",
                                    f"only {len(set(text))} unique chars in {n} "
                                    f"(ratio {ratio:.4f} < {_LOW_ENTROPY_RATIO_FAIL})"))

    stripped = text.strip()
    # STUB ERROR sentinel from xscore extraction — surfaces wherever it appears
    # (parent stems too, not just leaves). A parent STUB ERROR loses critical
    # context for downstream consumers.
    if stripped == "STUB ERROR":
        issues.append(ScanIssue(qkey, is_leaf, "stub_error_warn", "warn",
                                f"text is 'STUB ERROR' sentinel (extraction failed for this node)"))
    elif is_leaf and marks > 0:
        if not stripped:
            issues.append(ScanIssue(qkey, is_leaf, "empty_with_marks_warn", "warn",
                                    f"marks={marks} but text is empty"))
        elif len(stripped) < _SHORT_TEXT_WARN:
            issues.append(ScanIssue(qkey, is_leaf, "short_text_warn", "warn",
                                    f"marks={marks} but text only {len(stripped)} chars: {stripped!r}"))
    return issues


def scan_paper(subject: str, bank_stem: str) -> PaperScan:
    """Scan one paper's bank YAML. Returns PaperScan with verdict + issues."""
    from eXam.bank import BANK_ROOT
    yaml_path = BANK_ROOT / subject / bank_stem / "exam_questions.yaml"
    issues: list[ScanIssue] = []
    leaf_count = 0
    total_chars = 0
    if not yaml_path.exists():
        issues.append(ScanIssue("(file)", False, "missing_fail", "fail",
                                f"no exam_questions.yaml at {yaml_path}"))
        return PaperScan(subject, bank_stem, yaml_path, 0, 0, issues)
    try:
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        issues.append(ScanIssue("(file)", False, "parse_fail", "fail",
                                f"YAML error: {exc}"))
        return PaperScan(subject, bank_stem, yaml_path, 0, 0, issues)

    qs = data.get("questions") or []
    if not qs:
        issues.append(ScanIssue("(file)", False, "no_questions_fail", "fail",
                                "questions list is empty"))
        return PaperScan(subject, bank_stem, yaml_path, 0, 0, issues)

    # Duplicate-qnum check per nesting level.
    def _check_dup(siblings: list[dict], scope: str) -> None:
        seen: dict[str, int] = defaultdict(int)
        for q in siblings:
            if not isinstance(q, dict):
                continue
            num = str(q.get("number") or "")
            seen[num] += 1
        for num, count in seen.items():
            if count > 1:
                issues.append(ScanIssue(num, False, "duplicate_qnum_fail", "fail",
                                        f"qnum {num!r} appears {count}× under {scope}"))
        for q in siblings:
            if isinstance(q, dict) and (q.get("subquestions") or []):
                _check_dup(q["subquestions"], scope=f"{scope}/{q.get('number')}")
    _check_dup(qs, scope="(top)")

    for qkey, q, is_leaf in _walk_questions(qs):
        if is_leaf:
            leaf_count += 1
        text = str(q.get("text") or "")
        total_chars += len(text)
        issues.extend(_check_node(qkey, q, is_leaf))

    return PaperScan(subject, bank_stem, yaml_path, leaf_count, total_chars, issues)


def scan_subject(subject: str) -> list[PaperScan]:
    """Scan every indexed paper for *subject*. Returns list of PaperScan."""
    from eXam.bank import BANK_ROOT
    subj_dir = BANK_ROOT / subject
    if not subj_dir.exists():
        return []
    out: list[PaperScan] = []
    for entry in sorted(subj_dir.iterdir()):
        if entry.is_dir() and (entry / "exam_questions.yaml").exists():
            out.append(scan_paper(subject, entry.name))
    return out


def write_scan_report(subject: str, scans: list[PaperScan]) -> Path:
    QUALITY_REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    path = QUALITY_REVIEWS_DIR / f"scan_{subject}_{ts}.md"
    n_ok = sum(1 for s in scans if s.verdict == "ok")
    n_warn = sum(1 for s in scans if s.verdict == "warn")
    n_fail = sum(1 for s in scans if s.verdict == "fail")
    lines = [
        f"# Scan — {subject} — {ts}",
        "",
        f"- Total papers scanned: **{len(scans)}**",
        f"- OK: **{n_ok}**, WARN: **{n_warn}**, FAIL: **{n_fail}**",
        "",
        "## Per-paper verdicts",
        "",
    ]
    for s in scans:
        marker = {"ok": "OK", "warn": "WARN", "fail": "FAIL"}[s.verdict]
        lines.append(f"- **[{marker}]** `{s.bank_stem}` — {s.leaf_count} leaves, "
                     f"{s.total_text_chars} total chars")
    lines.append("")
    flagged = [s for s in scans if s.verdict != "ok"]
    if flagged:
        lines.append("## Issues")
        lines.append("")
        for s in flagged:
            lines.append(f"### `{s.bank_stem}`  (verdict: **{s.verdict.upper()}**)")
            for i in s.issues:
                node_kind = "leaf" if i.is_leaf else "node"
                lines.append(f"- q=`{i.qkey}` ({node_kind}): **{i.severity.upper()}** "
                             f"{i.rule} — {i.detail}")
            lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ── State file ───────────────────────────────────────────────────────────


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"runs": []}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"runs": []}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _record_run(
    *, subject: str, max_papers: int, outcomes: list[tuple[PaperRef, ExtractOutcome]],
    elapsed_s: float,
) -> None:
    state = _load_state()
    state["runs"].append({
        "subject": subject,
        "max_papers": max_papers,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(elapsed_s, 1),
        "outcomes": {p.bank_stem: o.value for p, o in outcomes},
    })
    _save_state(state)


# ── Driver ───────────────────────────────────────────────────────────────


def run_subject(
    subject: str, *, max_papers: int = 30, dry_run: bool = False,
    workers: int = 1,
) -> list[tuple[PaperRef, ExtractOutcome]]:
    all_papers = discover(subject)
    if not all_papers:
        print(f"{subject}: no question-paper PDFs found in exams/{subject}/", file=sys.stderr)
        return []
    selected = select_for_mass(all_papers, subject, max_papers)

    # Family summary so the human sees what they're getting.
    fam_counts: dict[int, int] = defaultdict(int)
    for p in selected:
        fam_counts[p.family] += 1
    fam_str = ", ".join(f"{f}x:{n}" for f, n in sorted(fam_counts.items()))
    print(f"{subject}: discovered {len(all_papers)}, selected {len(selected)}  ({fam_str})",
          file=sys.stderr)

    if dry_run:
        print(f"\n--- DRY-RUN: would extract {len(selected)} papers ---", file=sys.stderr)
        for p in selected:
            _, ypath = _bank_paths(p)
            status = "skip (already in bank)" if ypath.exists() else "extract"
            print(f"  [{status}] {p.year}-{p.month:02d} v{p.variant:02d}  {p.bank_stem}",
                  file=sys.stderr)
        return [(p, ExtractOutcome.SKIPPED) for p in selected]

    todo = [p for p in selected if not _bank_paths(p)[1].exists()]
    if not todo:
        print(f"{subject}: all {len(selected)} selected papers already in bank — nothing to extract",
              file=sys.stderr)
        return [(p, ExtractOutcome.SKIPPED) for p in selected]
    print(f"{subject}: {len(selected) - len(todo)} skipped, {len(todo)} to extract "
          f"(workers={workers})", file=sys.stderr)

    t0 = time.monotonic()
    outcomes: list[tuple[PaperRef, ExtractOutcome]] = [
        (p, ExtractOutcome.SKIPPED) for p in selected if p not in todo
    ]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(extract_one, p): p for p in todo}
        done = 0
        for fut in as_completed(futures):
            p = futures[fut]
            done += 1
            try:
                outcome = fut.result()
            except Exception as exc:  # noqa: BLE001
                outcome = ExtractOutcome.FAILED
                print(f"  [{done}/{len(todo)}] CRASH {p.bank_stem}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
            else:
                tag = {"extracted": "ok", "skipped": "skip", "failed": "FAIL"}[outcome.value]
                print(f"  [{done}/{len(todo)}] {tag}  {p.bank_stem}", file=sys.stderr)
            outcomes.append((p, outcome))
    elapsed = time.monotonic() - t0
    _record_run(subject=subject, max_papers=max_papers, outcomes=outcomes, elapsed_s=elapsed)
    n_ok = sum(1 for _, o in outcomes if o == ExtractOutcome.EXTRACTED)
    n_fail = sum(1 for _, o in outcomes if o == ExtractOutcome.FAILED)
    print(f"\n{subject}: {n_ok} extracted, {n_fail} failed  ({elapsed:.0f}s)",
          file=sys.stderr)
    return outcomes


def retry_paper(bank_stem: str) -> bool:
    """Find the paper across all subjects, wipe its bank dir, re-extract.

    Returns True on a successful re-extraction.
    """
    for subject in EXAM_ROOT_BY_KEY:
        for p in discover(subject):
            if p.bank_stem != bank_stem:
                continue
            bank_dir, _ = _bank_paths(p)
            print(f"retry: wiping {bank_dir}", file=sys.stderr)
            _wipe_bank_dir(bank_dir)
            outcome = extract_one(p)
            print(f"retry: {p.bank_stem} → {outcome.value}", file=sys.stderr)
            return outcome == ExtractOutcome.EXTRACTED
    print(f"retry: no paper with stem {bank_stem!r} found in any subject", file=sys.stderr)
    return False


# ── CLI ──────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    from eXercise.env_load import load_project_env
    load_project_env()

    p = argparse.ArgumentParser(prog="python -m web.extract_papers", description=__doc__)
    p.add_argument("--subject", help="Subject key (e.g. igcse_computer_science)")
    p.add_argument("--max-papers", type=int, default=30)
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be extracted; no API calls")
    p.add_argument("--workers", type=int,
                   default=int(os.environ.get("EXAM_EXTRACT_WORKERS", "1") or "1"),
                   help="Concurrent papers (default 1 — serial; bigger values risk "
                        "burning tokens on systemic failures before they're noticed)")
    p.add_argument("--retry-paper", metavar="STEM",
                   help="Delete that paper's bank dir and re-extract once")
    p.add_argument("--scan", action="store_true",
                   help="Run the deterministic quality scan for --subject "
                        "(or all bank subjects if --subject is omitted) "
                        "and write a scan_<subject>_<ts>.md report")
    args = p.parse_args(argv)

    if args.retry_paper:
        return 0 if retry_paper(args.retry_paper) else 1

    if args.scan:
        from eXam.bank import BANK_ROOT
        if args.subject:
            subjects = [args.subject]
        else:
            subjects = sorted(d.name for d in BANK_ROOT.iterdir() if d.is_dir()) \
                if BANK_ROOT.exists() else []
        for subj in subjects:
            scans = scan_subject(subj)
            if not scans:
                print(f"{subj}: no indexed papers", file=sys.stderr)
                continue
            path = write_scan_report(subj, scans)
            n_ok = sum(1 for s in scans if s.verdict == "ok")
            n_warn = sum(1 for s in scans if s.verdict == "warn")
            n_fail = sum(1 for s in scans if s.verdict == "fail")
            print(f"{subj}: {len(scans)} scanned (ok={n_ok} warn={n_warn} fail={n_fail}) → {path}",
                  file=sys.stderr)
        return 0

    if not args.subject:
        print("error: --subject required (unless --retry-paper or --scan)", file=sys.stderr)
        return 2
    if args.subject not in EXAM_ROOT_BY_KEY:
        print(f"unknown subject: {args.subject!r}", file=sys.stderr)
        print(f"available: {', '.join(EXAM_ROOT_BY_KEY)}", file=sys.stderr)
        return 2

    outcomes = run_subject(
        args.subject, max_papers=args.max_papers,
        dry_run=args.dry_run, workers=args.workers,
    )
    n_fail = sum(1 for _, o in outcomes if o == ExtractOutcome.FAILED)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
