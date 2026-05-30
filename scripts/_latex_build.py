# -*- coding: utf-8 -*-
"""Shared xelatex compile core for the handout / vocab PDF builders.

Stdlib-only; both ``scripts/build_handout_pdf.py`` and ``scripts/build_vocab_pdf.py``
import these so the compile path (throwaway temp dir, ``SOURCE_DATE_EPOCH`` pinning,
wall-clock timeout, log capture) lives in one place. Invoked via ``python -m scripts.X``,
so ``from scripts._latex_build import …`` resolves on ``sys.path`` (no ``__init__.py``).
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def find_xelatex() -> str | None:
    for c in ("/Library/TeX/texbin/xelatex", "/usr/local/bin/xelatex", "/usr/bin/xelatex"):
        if Path(c).is_file():
            return c
    return shutil.which("xelatex")


def source_date_epoch(meta: dict) -> str:
    """Stable PDF timestamp from the content stamp → no git churn on rebuild."""
    stamp = meta.get("glossed_at") or meta.get("generated_at") or "2020-01-01T00:00:00Z"
    try:
        d = _dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc)
        return str(int(d.timestamp()))
    except (ValueError, TypeError):
        return "1577836800"  # 2020-01-01T00:00:00Z


def compile_tex(
    tex_file: Path,
    out_pdf: Path,
    *,
    sde: str,
    log_target: Path,
    prefix: str = "latex_",
) -> tuple[bool, str]:
    """Compile *tex_file* → *out_pdf* in a throwaway temp dir. Returns (ok, error)."""
    xelatex = find_xelatex()
    if not xelatex:
        return False, "xelatex not found"
    env = {**os.environ, "SOURCE_DATE_EPOCH": sde, "FORCE_SOURCE_DATE": "1"}
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
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
