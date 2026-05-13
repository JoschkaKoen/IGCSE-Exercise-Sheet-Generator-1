"""Resume-from-step support: bootstrap ``ctx`` from a prior run's artifacts.

``resume_pipeline`` is invoked from inside ``locate_exam_folder`` once
``ctx.artifact_dir`` exists; ``copy_input_files`` is called from the same
step to mirror exam/scan/answer/roster files into the new artifact dir.
``copy_artifacts`` is internal to ``resume_pipeline``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from xscore.shared.pipeline_ctx import _Ctx


# ---------------------------------------------------------------------------
# Input-file copy helper
# ---------------------------------------------------------------------------

def copy_input_files(folder: Path, artifact_dir: Path) -> None:
    """Copy all input files used by this run into ``artifact_dir/input/``.

    Uses the same file-matching rules as ``validate_input_files`` so every
    file that the pipeline reads is preserved alongside the artifacts.
    """
    from xscore.shared.exam_paths import artifact_input_dir
    dst = artifact_input_dir(artifact_dir)
    dst.mkdir(parents=True, exist_ok=True)
    from xscore.shared.step_folders import CLEANED_SCAN_PDF
    _EXAM_SKIP = ("scan", "answer", "student", "cleaned", "_ms_")
    for f in folder.iterdir():
        if not f.is_file():
            continue
        if (f.suffix.lower() == ".pdf"
                and "scan" in f.name.lower()
                and "cleaned" not in f.name.lower()
                and f.name != CLEANED_SCAN_PDF):
            shutil.copy2(f, dst / f.name)
            continue
        if f.suffix.lower() == ".pdf" and not any(kw in f.name.lower() for kw in _EXAM_SKIP):
            shutil.copy2(f, dst / f.name)
            continue
        if f.suffix.lower() == ".pdf" and ("answer" in f.name.lower() or "_ms_" in f.name.lower()):
            shutil.copy2(f, dst / f.name)
            continue
        if any(kw in f.name.lower() for kw in ("studentlist", "student", "roster")) and f.suffix.lower() in (".xlsx", ".xls", ".csv", ".txt"):
            shutil.copy2(f, dst / f.name)
            continue


# ---------------------------------------------------------------------------
# Resume-from-step helpers
# ---------------------------------------------------------------------------

def copy_artifacts(src: Path, dst: Path, from_step: int) -> None:
    """Copy prior-run artifacts needed for resuming from *from_step* into *dst*.

    Patterns are derived from ``pipeline_steps.STEPS[*].writes`` for every step
    *before* *from_step* (those outputs become inputs for the resumed run).
    The merged/deskewed scan PDF lives at the run root and is also copied.
    """
    from xscore.shared.pipeline_steps import STEPS

    patterns: list[str] = [g for s in STEPS if s.number < from_step for g in s.writes]
    patterns.append("scanned_exam_merged_and_angles_adjusted.pdf")
    for pat in patterns:
        for src_path in src.glob(pat):
            dst_path = dst / src_path.relative_to(src)
            if src_path.is_dir():
                shutil.copytree(src_path, dst_path, dirs_exist_ok=True, copy_function=shutil.copy2)
            else:
                dst_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_path, dst_path)


def resume_pipeline(ctx: "_Ctx") -> None:
    """Bootstrap *ctx* from a prior run's artifacts and set ctx.from_step skip logic."""
    from xscore.shared.exam_paths import (
        artifact_exam_student_list_json_path,
        artifact_geometry_json_path,
        artifact_students_json_path,
        is_completed_run,
    )
    from xscore.shared.models import PageAssignment as _PA
    from xscore.shared.pipeline_steps import resumable_step_numbers, step_by_name
    from xscore.shared.step_folders import (
        AI_MARKING_DIR,
        BLUEPRINTS_DIR,
        CREATE_REPORT_DIR,
        STUDENT_LIST_DIR,
        STUDENT_NAMES_DIR,
    )
    from xscore.shared.terminal_ui import ok_line
    from xscore.scaffold.generate_scaffold import build_scaffold

    resume_dir = ctx.resume_dir
    if resume_dir is None:
        assert ctx.folder is not None
        exam_output_root = Path("output") / "xscore" / ctx.folder.name.replace(" ", "_")
        candidates = sorted(
            (p for p in exam_output_root.iterdir()
             if p.is_dir() and p != ctx.artifact_dir and is_completed_run(p)),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not candidates:
            raise SystemExit(
                f"No valid prior runs found in {exam_output_root}. Use --resume-dir."
            )
        resume_dir = candidates[0]
    ctx.resume_dir = resume_dir

    valid_steps = resumable_step_numbers()
    if ctx.from_step not in valid_steps:
        raise SystemExit(
            f"--from-step {ctx.from_step} not supported for this run "
            f"(use {', '.join(str(s) for s in valid_steps)})."
        )

    # Blueprints and marking step numbers — looked up by name so renumbers
    # don't break the "do we need this artifact?" logic.
    _blueprints_n = step_by_name("ai_marking_blueprints").number
    _marking_n = step_by_name("ai_marking").number

    required: list[Path] = [
        resume_dir / "scanned_exam_merged_and_angles_adjusted.pdf",
        resume_dir / STUDENT_LIST_DIR / "students.json",
        resume_dir / STUDENT_NAMES_DIR / "exam_student_list.json",
        resume_dir / CREATE_REPORT_DIR / "report.yaml",
    ]

    missing_globs: list[str] = []
    if ctx.from_step > _blueprints_n:
        bp_found = list(resume_dir.glob(f"{BLUEPRINTS_DIR}/blueprint_page_*.yaml"))
        if bp_found:
            required += bp_found
        else:
            missing_globs.append(f"blueprint_page_*.yaml (in {BLUEPRINTS_DIR}/)")
    if ctx.from_step > _marking_n:
        mk_found = list(resume_dir.glob(f"{AI_MARKING_DIR}/students/*/page_*.yaml"))
        if mk_found:
            required += mk_found
        else:
            missing_globs.append(f"students/*/page_*.yaml (in {AI_MARKING_DIR}/)")
    missing = [p.name for p in required if not p.exists()] + missing_globs
    if missing:
        raise SystemExit(
            f"Prior run {resume_dir} is missing required artifacts:\n"
            + "\n".join(f"  {m}" for m in missing)
        )

    assert ctx.artifact_dir is not None
    copy_artifacts(resume_dir, ctx.artifact_dir, ctx.from_step)

    ctx.cleaned_pdf = ctx.artifact_dir / "scanned_exam_merged_and_angles_adjusted.pdf"

    ctx.students = json.loads(artifact_students_json_path(ctx.artifact_dir).read_text())

    _raw_pa = json.loads(artifact_exam_student_list_json_path(ctx.artifact_dir).read_text())
    ctx.page_assignments = [
        _PA(
            student_name=a["student_name"],
            page_numbers=a["page_numbers"],
            confidence=a["confidence"],
            cover_page_number=a.get("cover_page_number"),
        )
        for a in _raw_pa
    ]
    ctx.num_students = len(ctx.page_assignments)
    ctx.pages_per_student = max(
        (len(a.page_numbers) for a in ctx.page_assignments), default=0
    )

    geo_path = artifact_geometry_json_path(ctx.artifact_dir)
    if geo_path.exists():
        geo = json.loads(geo_path.read_text())
        ctx.empty_exam_has_cover = geo.get("empty_exam_has_cover")
        ctx.cover_page_mode = bool(geo.get("scan_has_cover", False))

    from xscore.shared.exam_paths import artifact_subject_json_path
    subject_path = artifact_subject_json_path(ctx.artifact_dir)
    if subject_path.exists():
        from xscore.shared.subjects import get_subject
        raw_subj = json.loads(subject_path.read_text(encoding="utf-8"))
        try:
            ctx.subject = get_subject(raw_subj["name"])
        except KeyError:
            pass  # subject removed from KNOWN_SUBJECTS since the prior run; skip

    ctx.scaffold = build_scaffold(
        ctx.folder, artifact_dir=ctx.artifact_dir, force_rebuild=False
    )

    ok_line(f"Resumed from  {resume_dir}  (from-step {ctx.from_step})")


# ---------------------------------------------------------------------------
# Helpers that do not depend on deferred imports
# ---------------------------------------------------------------------------

def exam_pdf_page_count(folder: Path) -> int:
    """Count pages in the exam PDF without building the scaffold."""
    import fitz
    from xscore.scaffold.generate_scaffold import find_exam_pdf
    with fitz.open(str(find_exam_pdf(folder))) as doc:
        return doc.page_count
