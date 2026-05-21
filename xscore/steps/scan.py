"""Scan-cleaning step bodies: roster, optional duplex merge, deskew.

``prepare_scans`` is conditional on whether the scan folder contains numbered duplex
pairs (``find_scan_pairs``). One pair (scan1+scan2) or many (scan1+scan2,
scan3+scan4, ...) all flow through the same merge into one ``merged_scan.pdf``.
The single-PDF branch (no numbered files; falls through to
``find_source_scan_match``) is silent — no header, no run-log entry, no
summary.json. The conditional dispatch lives in ``scan_phases``; the runner
calls that helper rather than looping over the scan-cleaning steps individually.

The deskew step writes a per-step ``summary.json`` whose ``elapsed_s`` field
is captured locally (run_step's outer timing only becomes available *after*
the body returns).
"""

from __future__ import annotations

import json
import time

from xscore.config import PIPELINE_DEFAULT_DPI
from xscore.preprocessing.coordinator import (
    deskew_phase,
    prepare_scans_phase,
)
from xscore.shared.step_folders import DESKEW_DIR
from xscore.shared.load_student_list import read_student_list as _read_student_list
from xscore.shared.pipeline_ctx import _Ctx
from xscore.shared.pipeline_steps import run_step, step_by_name
from xscore.shared.student_artifacts import write_student_artifacts
from xscore.shared.terminal_ui import (
    announce_step_model,
    format_duration,
    ok_line,
)


def read_student_list(ctx: _Ctx) -> None:
    assert ctx.folder is not None and ctx.artifact_dir is not None
    announce_step_model(
        model_env="READ_STUDENT_LIST_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_max_tokens=2048,
    )
    ctx.students = _read_student_list(ctx.folder, ctx.artifact_dir)
    ok_line(f"{len(ctx.students)} students on the roster")
    write_student_artifacts(ctx.artifact_dir, ctx.students)


def prepare_scans(ctx: _Ctx) -> None:
    """Orient + (optionally) merge scans. Always runs.

    Detects per-file orientation via Qwen vision (page 1 at 300 DPI), then
    either interleaves duplex pairs or writes a rotated single-PDF copy.
    See ``xscore.preprocessing.coordinator.prepare_scans_phase``.
    """
    assert ctx.folder is not None and ctx.artifact_dir is not None and ctx.instruction is not None
    t0 = time.perf_counter()
    ctx.scan_match = prepare_scans_phase(
        ctx.folder,
        ctx.artifact_dir,
        PIPELINE_DEFAULT_DPI,
        force_rebuild=ctx.force_clean_scan,
    )
    ok_line(f"Scans prepared  ·  {format_duration(time.perf_counter() - t0)}")


def deskew(ctx: _Ctx) -> None:
    assert ctx.folder is not None and ctx.artifact_dir is not None and ctx.instruction is not None
    t0 = time.perf_counter()
    ctx.cleaned_pdf = deskew_phase(
        ctx.artifact_dir, PIPELINE_DEFAULT_DPI, input_pdf=ctx.scan_match,
    )
    p = ctx.artifact_dir / DESKEW_DIR / "summary.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(
            {
                "step": step_by_name("deskew").number,
                "elapsed_s": round(time.perf_counter() - t0, 3),
                "status": "ok",
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def scan_phases(ctx: _Ctx) -> None:
    """Scan-cleaning sequence. ``prepare_scans`` always runs (per-file
    orientation detection + optional duplex merge); both single-PDF and duplex
    flows go through the same ``prepare_scans_phase``.

    Skipped entirely when resuming (``ctx.from_step`` set).
    """
    assert ctx.folder is not None and ctx.artifact_dir is not None and ctx.instruction is not None
    if ctx.from_step:
        return

    run_step(ctx, step_by_name("prepare_scans"))
    run_step(ctx, step_by_name("deskew"))
