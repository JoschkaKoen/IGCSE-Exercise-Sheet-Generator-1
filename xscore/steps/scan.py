"""Steps 3–7: roster, optional duplex merge, blank detection, autorotate, deskew.

Step 4 is conditional on whether the scan folder contains numbered duplex
pairs (``find_scan_pairs``). One pair (scan1+scan2) or many (scan1+scan2,
scan3+scan4, ...) all flow through the same merge into one ``merged_scan.pdf``.
The single-PDF branch (no numbered files; falls through to
``find_source_scan_match``) is silent — no header, no run-log entry, no
summary.json. The conditional dispatch lives in ``scan_phases``; the runner
calls that helper rather than looping over steps 4–7 individually.

Steps 5, 6, 7 each write a per-step ``summary.json`` whose ``elapsed_s`` field
is captured locally (run_step's outer timing only becomes available *after*
the body returns).
"""

from __future__ import annotations

import json
import shutil
import time

from xscore.config import ROTATION_ANALYSIS_DPI
from xscore.preprocessing.coordinator import (
    _STEP_05,
    _STEP_06,
    _STEP_07,
    autorotate_phase,
    deskew_phase,
    detect_blank_pages_phase,
    prepare_scans_phase,
)
from xscore.shared.load_student_list import read_student_list
from xscore.shared.pipeline_ctx import _Ctx, _EarlyExit
from xscore.shared.pipeline_steps import run_step, step_by_number
from xscore.shared.student_artifacts import write_student_artifacts
from xscore.shared.terminal_ui import announce_step_model, ok_line


def step_03_students(ctx: _Ctx) -> None:
    assert ctx.folder is not None and ctx.artifact_dir is not None
    announce_step_model(
        model_env="READ_STUDENT_LIST_MODEL",
        legacy_model_env="AI_DEFAULT_MODEL",
        default_max_tokens=2048,
    )
    ctx.students = read_student_list(ctx.folder, ctx.artifact_dir)
    ok_line(f"{len(ctx.students)} students on the roster")
    write_student_artifacts(ctx.artifact_dir, ctx.students)


def step_04_prepare(ctx: _Ctx) -> None:
    """Orient + (optionally) merge scans. Always runs.

    Detects per-file orientation via Qwen vision (page 1 at 300 DPI), then
    either interleaves duplex pairs or writes a rotated single-PDF copy.
    See ``xscore.preprocessing.coordinator.prepare_scans_phase``.
    """
    assert ctx.folder is not None and ctx.artifact_dir is not None and ctx.instruction is not None
    ctx.scan_match = prepare_scans_phase(
        ctx.folder,
        ctx.artifact_dir,
        ctx.instruction.dpi,
        force_rebuild=ctx.force_clean_scan,
    )


def step_05_blanks(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None and ctx.scan_match is not None
    t0 = time.perf_counter()
    detect_blank_pages_phase(
        ctx.scan_match, ctx.artifact_dir,
        analysis_dpi=ROTATION_ANALYSIS_DPI, force_clean_scan=ctx.force_clean_scan,
    )
    p = ctx.artifact_dir / _STEP_05 / "summary.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"step": 5, "elapsed_s": round(time.perf_counter() - t0, 3), "status": "ok"}, indent=2),
        encoding="utf-8",
    )


def step_06_rotate(ctx: _Ctx) -> None:
    assert ctx.artifact_dir is not None
    t0 = time.perf_counter()
    autorotate_phase(ctx.artifact_dir)
    p = ctx.artifact_dir / _STEP_06 / "summary.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"step": 6, "elapsed_s": round(time.perf_counter() - t0, 3), "status": "ok"}, indent=2),
        encoding="utf-8",
    )


def step_07_deskew(ctx: _Ctx) -> None:
    assert ctx.folder is not None and ctx.artifact_dir is not None and ctx.instruction is not None
    t0 = time.perf_counter()
    ctx.cleaned_pdf = deskew_phase(ctx.artifact_dir, ctx.instruction.dpi)
    shutil.copy2(ctx.cleaned_pdf, ctx.artifact_dir / ctx.cleaned_pdf.name)
    p = ctx.artifact_dir / _STEP_07 / "summary.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"step": 7, "elapsed_s": round(time.perf_counter() - t0, 3), "status": "ok"}, indent=2),
        encoding="utf-8",
    )


def scan_phases(ctx: _Ctx) -> None:
    """Steps 4–7. Step 4 always runs (per-file orientation detection +
    optional duplex merge); both single-PDF and duplex flows go through the
    same ``prepare_scans_phase``.

    Skipped entirely when resuming (``ctx.from_step`` set).
    """
    assert ctx.folder is not None and ctx.artifact_dir is not None and ctx.instruction is not None
    if ctx.from_step:
        return

    run_step(ctx, step_by_number(4))
    if ctx.stop_after <= 4:
        raise _EarlyExit()

    run_step(ctx, step_by_number(5))
    run_step(ctx, step_by_number(6))
    run_step(ctx, step_by_number(7))
