# -*- coding: utf-8 -*-
"""Build printable PDFs from handout markdown via xelatex.

Deterministic, no AI: each ``output/eXam/handouts/<subject>/<NN>.md`` is converted
to ``pdf/<NN>.tex`` (web.handout_latex) and compiled to ``pdf/<NN>.pdf``. Both are
committed; the website keeps serving the live markdown.

Usage:
  .venv/bin/python -m scripts.build_handout_pdf <subject> [topic]   # one subject, or one NN
  .venv/bin/python -m scripts.build_handout_pdf --all               # all TARGET_SUBJECTS
  .venv/bin/python -m scripts.build_handout_pdf <subject> [topic] --no-regen  # compile existing .tex as-is

A ``% handout-pdf: manual`` sentinel on line 1 of a ``.tex`` protects hand-edits:
that file is never regenerated (checked on every path); ``--no-regen`` is a debug
convenience that compiles the existing ``.tex`` without re-running the converter.
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from web.handout_latex import build_document
from web.handouts_collect import (
    TARGET_SUBJECTS,
    handout_dir,
    load_meta,
    logs_dir,
    md_path,
    meta_path,
    padded_topic,
    pdf_path,
    tex_path,
)

MANUAL_SENTINEL = "% handout-pdf: manual"


def _find_xelatex() -> str | None:
    for c in ("/Library/TeX/texbin/xelatex", "/usr/local/bin/xelatex", "/usr/bin/xelatex"):
        if Path(c).is_file():
            return c
    return shutil.which("xelatex")


def _source_date_epoch(meta: dict) -> str:
    """Stable PDF timestamp from the content stamp → no git churn on rebuild."""
    stamp = meta.get("glossed_at") or meta.get("generated_at") or "2020-01-01T00:00:00Z"
    try:
        d = _dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
        return str(int(d.timestamp()))
    except (ValueError, TypeError):
        return "1577836800"  # 2020-01-01T00:00:00Z


def _topics(subject: str) -> list[str]:
    return sorted(p.stem for p in handout_dir(subject).glob("[0-9][0-9].md"))


def _is_manual(tex_file: Path) -> bool:
    if not tex_file.is_file():
        return False
    try:
        with tex_file.open(encoding="utf-8") as f:
            return f.readline().strip() == MANUAL_SENTINEL
    except OSError:
        return False


def _compile(tex_file: Path, out_pdf: Path, *, sde: str, log_target: Path) -> tuple[bool, str]:
    xelatex = _find_xelatex()
    if not xelatex:
        return False, "xelatex not found"
    env = {**os.environ, "SOURCE_DATE_EPOCH": sde, "FORCE_SOURCE_DATE": "1"}
    with tempfile.TemporaryDirectory(prefix="handout_pdf_") as tmp:
        tmp_path = Path(tmp)
        work = tmp_path / tex_file.name
        work.write_text(tex_file.read_text(encoding="utf-8"), encoding="utf-8")
        cmd = [xelatex, "-interaction=nonstopmode", f"-output-directory={tmp_path}", str(work)]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120, cwd=str(tmp_path), env=env
            )
        except subprocess.TimeoutExpired:
            return False, "xelatex timed out (120s)"
        except OSError as exc:
            return False, f"xelatex error: {exc}"
        produced = tmp_path / (work.stem + ".pdf")
        if not produced.is_file():
            try:
                log_target.parent.mkdir(parents=True, exist_ok=True)
                log_target.write_text(result.stdout or "", encoding="utf-8")
                loc = f" (full log: {log_target})"
            except OSError:
                loc = ""
            return False, f"no PDF produced{loc}\n     …{(result.stdout or '')[-900:]}"
        out_pdf.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(produced), str(out_pdf))
        return True, ""


def _build_one(subject: str, topic: str, *, no_regen: bool) -> tuple[bool, list[str], str]:
    tex_file = tex_path(subject, topic)
    meta = load_meta(meta_path(subject, topic))
    warnings: list[str] = []
    protected = _is_manual(tex_file)
    if not no_regen and not protected:
        md_file = md_path(subject, topic)
        if not md_file.is_file():
            return False, warnings, "no .md source"
        tex, warnings = build_document(
            md_file.read_text(encoding="utf-8"), subject=subject, topic=topic, meta=meta
        )
        tex_file.parent.mkdir(parents=True, exist_ok=True)
        tex_file.write_text(tex, encoding="utf-8")
    elif not tex_file.is_file():
        return False, warnings, "no .tex to compile (run without --no-regen first)"
    ok, err = _compile(
        tex_file,
        pdf_path(subject, topic),
        sde=_source_date_epoch(meta),
        log_target=logs_dir(subject, topic) / "xelatex.log",
    )
    if protected and ok:
        warnings.append("manual .tex (not regenerated)")
    return ok, warnings, err


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    no_regen = "--no-regen" in argv
    argv = [a for a in argv if a != "--no-regen"]

    if argv == ["--all"]:
        targets = [(s, t) for s in TARGET_SUBJECTS for t in _topics(s)]
    elif argv and not argv[0].startswith("--"):
        subject = argv[0]
        if not handout_dir(subject).is_dir():
            print(f"no such subject dir: {handout_dir(subject)}", file=sys.stderr)
            return 2
        targets = [(subject, argv[1])] if len(argv) > 1 else [(subject, t) for t in _topics(subject)]
    else:
        print(__doc__, file=sys.stderr)
        return 2

    failures = warned = 0
    for subject, topic in targets:
        ok, warnings, err = _build_one(subject, topic, no_regen=no_regen)
        pt = padded_topic(topic)
        if ok:
            warned += 1 if warnings else 0
            suffix = f"  [{'; '.join(warnings)}]" if warnings else ""
            print(f"{'✓!' if warnings else '✓'} {subject}/{pt}.pdf{suffix}")
        else:
            failures += 1
            print(f"✗ {subject}/{pt}.md  — {err}")
    print(
        f"\nBuilt {len(targets) - failures}/{len(targets)} handout(s); "
        f"{failures} failed; {warned} with warnings."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
