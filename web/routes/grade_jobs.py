# -*- coding: utf-8 -*-
"""Grade pipeline jobs: upload exam scans, run the canonical xScore pipeline, return artifacts.

The pipeline orchestration lives in ``xscore.pipeline.runner.run_pipeline`` —
this module is the FastAPI front door that builds the request shape, dispatches
the worker, and exposes per-artifact downloads. All pipeline steps + resume
support come from the canonical runner; this module owns no step logic of its own.
"""

from __future__ import annotations

import asyncio
import datetime
import io
import logging
import os
import re
import shutil
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

from .._state import create_background_task, store
from ..grade_auth import require_grade_unlock
from ..grade_service import GradeFormOpts
from ..grade_subprocess import cancel_process, run_grade_subprocess
from ..jobs import JobStatus

router = APIRouter()


_GRADE_UPLOADS_ROOT = (
    Path(__file__).resolve().parent.parent.parent / "output" / "xscore" / "grade_uploads"
)
_OUTPUT_XSCORE_ROOT = Path(__file__).resolve().parent.parent.parent / "output" / "xscore"

# ANSI CSI + OSC strippers — the subprocess sees a pipe (non-TTY) so Rich auto-
# disables colors, but some libraries emit ANSI unconditionally; strip defensively.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*\x07")


def _strip_ansi(line: str) -> str:
    return _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", line)).rstrip("\r")


# ---------------------------------------------------------------------------
# Step registry — driven by the canonical xscore registry, with section
# fill-forward (the registry only sets ``section`` on the first step of each
# phase boundary; subsequent steps inherit it).
# ---------------------------------------------------------------------------

def _build_grade_steps() -> list[tuple[int, str, str | None]]:
    from xscore.shared.pipeline_steps import STEPS, wire_step_fns
    wire_step_fns()  # idempotent; binds step bodies so step.title fallback works

    out: list[tuple[int, str, str | None]] = []
    section: str | None = None
    for s in STEPS:
        if s.section:
            section = s.section
        title = s.title or s.name.replace("_", " ").capitalize()
        out.append((s.number, title, section))
    return out


_GRADE_STEPS = _build_grade_steps()


# ---------------------------------------------------------------------------
# Worker — spawn xScore as a subprocess, route stdout into JobStore.
# ---------------------------------------------------------------------------

async def _run_grade_job(
    job_id: str,
    folder: Path,
    opts: GradeFormOpts,
) -> None:
    store.set_status(job_id, JobStatus.RUNNING)
    store.init_steps(job_id, _GRADE_STEPS)
    store.set_log_line(job_id, "Starting pipeline…")
    store.set_upload_folder(job_id, folder)

    def on_line(raw: str) -> None:
        line = _strip_ansi(raw)
        if not line.strip():
            return
        store.append_log_line(job_id, line)
        store.set_log_line(job_id, line)

    def on_event(evt: dict) -> None:
        n = evt.get("step_number")
        status = evt.get("status")
        if n is None or status is None:
            return
        if status == "running":
            store.step_running(job_id, n)
            store.record_running_step(job_id, n)
        elif status == "ok":
            store.step_done(job_id, n, evt.get("duration_s") or 0.0)
            store.clear_running_step(job_id)
        elif status == "error":
            store.step_failed(job_id, n, evt.get("duration_s") or 0.0)
            store.clear_running_step(job_id)
        ad = evt.get("artifact_dir")
        if ad:
            store.set_artifact_dir(job_id, Path(ad))

    def register_proc(p) -> None:
        store.set_process(job_id, p)

    try:
        exit_code = await run_grade_subprocess(
            folder,
            opts,
            on_line=on_line,
            on_event=on_event,
            register_proc=register_proc,
        )
    except Exception as e:  # noqa: BLE001
        logging.exception("Grade pipeline spawn failed for job %s", job_id)
        store.fail(job_id, f"Pipeline spawn error: {e}")
        return

    # Cancel inference: POSIX signal-killed → exit_code < 0; shell-style signal
    # encoding → exit_code > 128. A clean 0 always wins, even if cancel was
    # requested mid-flight (race: pipeline finished before SIGTERM landed).
    was_signal_killed = exit_code < 0 or exit_code > 128
    current = store.get(job_id)
    if current is not None and current.status == JobStatus.CANCELED:
        # The cancel endpoint already marked it; nothing else to do.
        return

    if exit_code == 0:
        rec = store.get(job_id)
        artifact_dir = rec.artifact_dir if rec else None
        cleaned_pdf: Path | None = None
        class_report_pdf: Path | None = None
        if artifact_dir:
            from xscore.shared.exam_paths import (
                DESKEW_DIR,
                artifact_class_report_pdf_path,
            )
            for candidate in (
                artifact_dir / DESKEW_DIR / "cleaned_scan.pdf",
                artifact_dir / "cleaned_scan.pdf",
            ):
                if candidate.is_file():
                    cleaned_pdf = candidate
                    break
            cand = artifact_class_report_pdf_path(artifact_dir)
            if cand.is_file():
                class_report_pdf = cand
        store.complete(
            job_id,
            output_pdf=cleaned_pdf or folder,
            answers_pdf=class_report_pdf,
        )
    elif was_signal_killed:
        # Process was signaled but not via the /cancel endpoint (likely OS OOM
        # killer, external kill, etc.). Surface as canceled rather than failed
        # so the UI shows the right terminal state.
        store.cancel(job_id, message=f"Process terminated by signal (exit {exit_code})")
    else:
        rec = store.get(job_id)
        tail = "\n".join(rec.log_lines[-5:]) if rec and rec.log_lines else ""
        msg = f"Pipeline exited with code {exit_code}"
        if tail:
            msg += f"\n…\n{tail}"
        store.fail(job_id, msg)


# ---------------------------------------------------------------------------
# Form parsing + server-side validation
# ---------------------------------------------------------------------------

def _parse_students_csv(s: str | None) -> list[str] | None:
    if not s:
        return None
    names: list[str] = []
    for piece in s.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if not re.search(r"[A-Za-zÀ-￿]", piece):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid student name (no letters): {piece!r}",
            )
        names.append(piece)
    return names or None


def _validate_resume_dir(resume_dir_str: str | None) -> Path | None:
    if not resume_dir_str:
        return None
    p = Path(resume_dir_str).expanduser().resolve()
    try:
        p.relative_to(_OUTPUT_XSCORE_ROOT.resolve())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"resume_dir must live under output/xscore/ (got {p})",
        ) from None
    if not p.is_dir():
        raise HTTPException(status_code=400, detail=f"resume_dir does not exist: {p}")
    return p


def _validate_opts(opts: GradeFormOpts) -> None:
    """Fail fast on bad form input — the canonical pipeline would SystemExit later."""
    from xscore.shared.pipeline_steps import max_step_number, resumable_step_numbers

    max_n = max_step_number()
    if opts.stop_after is not None and not (1 <= opts.stop_after <= max_n):
        raise HTTPException(
            status_code=400,
            detail=f"stop_after must be in [1, {max_n}] (got {opts.stop_after})",
        )
    if opts.from_step is not None:
        valid = resumable_step_numbers()
        if opts.from_step not in valid:
            raise HTTPException(
                status_code=400,
                detail=f"from_step must be in {list(valid)} (got {opts.from_step})",
            )
    if opts.limit_students is not None and opts.limit_students < 1:
        raise HTTPException(
            status_code=400,
            detail=f"limit_students must be ≥ 1 (got {opts.limit_students})",
        )


# ---------------------------------------------------------------------------
# POST /api/grade/jobs — upload + start a fresh run
# ---------------------------------------------------------------------------

_SCAN_NUMBERED_RE = re.compile(r"^scan[\s_\-]*(\d+)\b", re.IGNORECASE)
_MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB


async def _read_limited(upload: UploadFile, name: str) -> bytes:
    cl = upload.headers.get("content-length")
    if cl and int(cl) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"{name} exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit",
        )
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{name} exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _make_unique_upload_folder() -> Path:
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    for _ in range(5):
        upload_id = f"{ts}_{os.urandom(8).hex()}"
        folder = _GRADE_UPLOADS_ROOT / upload_id
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=False)
            return folder
    raise RuntimeError("Could not create a unique upload directory")


@router.post("/api/grade/jobs")
async def create_grade_job(
    exam_scans: list[UploadFile] = File(...),
    student_list: UploadFile = File(...),
    empty_exam: UploadFile = File(...),
    answer_sheet: UploadFile = File(...),
    prompt: str | None = Form(None),
    force_clean_scan: bool = Form(False),
    stop_after: int | None = Form(None),
    from_step: int | None = Form(None),
    resume_dir: str | None = Form(None),
    students_csv: str | None = Form(None),
    limit_students: int | None = Form(None),
    use_cache: bool = Form(False),
    _gate: None = Depends(require_grade_unlock),
) -> dict[str, str]:
    """Accept uploaded exam files, save them, and launch an xScore pipeline job.

    *exam_scans* accepts either a single PDF (saved as ``scan.pdf``) or multiple
    PDFs whose original filenames must each match ``scan{N}`` with optional
    separator (``scan1.pdf``, ``scan 2.pdf``, ``scan_3.pdf``, ``scan-4.pdf``,
    ...). Each numbered file is saved canonically as ``scan{N}.pdf``.

    Advanced form fields mirror the ``XScore.py`` CLI flags. Validation fails
    fast with HTTPException(400) before launching the worker.
    """
    opts = GradeFormOpts(
        prompt=prompt,
        force_clean_scan=force_clean_scan,
        stop_after=stop_after,
        from_step=from_step,
        resume_dir=_validate_resume_dir(resume_dir),
        students=_parse_students_csv(students_csv),
        limit_students=limit_students,
        use_cache=use_cache,
    )
    _validate_opts(opts)

    folder = _make_unique_upload_folder()

    # Save scans (single PDF or numbered multi-PDF set).
    if len(exam_scans) == 1:
        (folder / "scan.pdf").write_bytes(await _read_limited(exam_scans[0], "exam_scans"))
    else:
        parsed: list[tuple[int, UploadFile]] = []
        for upload in exam_scans:
            m = _SCAN_NUMBERED_RE.match(upload.filename or "")
            if not m:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Multi-file scan upload requires every file to be named "
                        f"scan1.pdf, scan2.pdf, ... (separators allowed: scan_1.pdf, "
                        f"scan-1.pdf, 'scan 1.pdf'). Got: {upload.filename!r}."
                    ),
                )
            parsed.append((int(m.group(1)), upload))
        seen: set[int] = set()
        for idx, upload in parsed:
            if idx in seen:
                raise HTTPException(
                    status_code=400,
                    detail=f"Duplicate scan index {idx} in upload (two files map to scan{idx}.pdf).",
                )
            seen.add(idx)
            (folder / f"scan{idx}.pdf").write_bytes(
                await _read_limited(upload, f"exam_scans[{upload.filename}]")
            )

    sl_suffix = Path(student_list.filename or "StudentList.xlsx").suffix or ".xlsx"
    (folder / f"StudentList{sl_suffix}").write_bytes(
        await _read_limited(student_list, "student_list")
    )
    (folder / "empty_exam.pdf").write_bytes(await _read_limited(empty_exam, "empty_exam"))
    (folder / "answer_sheet.pdf").write_bytes(await _read_limited(answer_sheet, "answer_sheet"))

    job = store.create()
    create_background_task(_run_grade_job(job.id, folder, opts))
    return {"id": job.id}


# ---------------------------------------------------------------------------
# POST /api/grade/jobs/{job_id}/cancel — stop the subprocess immediately
# ---------------------------------------------------------------------------

@router.post("/api/grade/jobs/{job_id}/cancel")
async def cancel_grade_job(
    job_id: str,
    _gate: None = Depends(require_grade_unlock),
) -> dict[str, str]:
    """Cancel a running grade job. Sends SIGTERM to the subprocess group
    (escalates to SIGKILL after 3 s). Idempotent for terminal states."""
    rec = store.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if rec.status != JobStatus.RUNNING:
        return {"status": rec.status, "message": "Job is not running"}

    # Three cancel-during-spawn races:
    #   (a) subprocess spawned but register_proc hasn't fired yet → retry briefly
    #   (b) spawn itself raised (FileNotFoundError, EMFILE) → no proc ever appears;
    #       mark canceled anyway so the UI state matches user intent (the worker
    #       branch will set FAILED concurrently — both transitions are terminal
    #       and store.cancel is a no-op once status is terminal).
    #   (c) two simultaneous cancels → killpg twice is harmless; store.cancel is idempotent.
    proc = None
    for _ in range(10):  # 10 * 50ms = 500ms grace
        proc = store.get_process(job_id)
        if proc is not None:
            break
        await asyncio.sleep(0.05)

    if proc is None:
        store.cancel(job_id)
        return {"status": JobStatus.CANCELED, "message": "Process not yet running; cancel recorded"}

    # Set status BEFORE signaling so the worker's exit branch sees CANCELED and
    # skips the duplicate fail() path on its end.
    store.cancel(job_id)
    try:
        await cancel_process(proc)
    except Exception as exc:  # noqa: BLE001
        logging.warning("cancel_process failed for job %s: %s", job_id, exc)
    return {"status": JobStatus.CANCELED}


# ---------------------------------------------------------------------------
# POST /api/grade/jobs/resume — re-enter a failed/partial run from a step
# ---------------------------------------------------------------------------

@router.post("/api/grade/jobs/resume")
async def resume_grade_job(
    artifact_dir: str = Form(...),
    from_step: int = Form(...),
    prompt: str | None = Form(None),
    use_cache: bool = Form(False),
    _gate: None = Depends(require_grade_unlock),
) -> dict[str, str]:
    """Resume a prior run from a resumable step (see resumable_step_numbers())."""
    art = _validate_resume_dir(artifact_dir)
    assert art is not None  # validator raised if it was missing

    # Find the upload folder. The artifact_dir is at output/xscore/<upload_id>/<ts>/;
    # the original upload was at output/xscore/grade_uploads/<upload_id>/.
    upload_id = art.parent.name
    upload_folder = _GRADE_UPLOADS_ROOT / upload_id

    if not upload_folder.is_dir():
        # Restore from the artifact's input/ copy that copy_input_files preserved.
        from xscore.shared.exam_paths import artifact_input_dir
        input_subdir = artifact_input_dir(art)
        if not input_subdir.is_dir():
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot resume: original upload folder {upload_folder} is missing "
                    f"and no input/ copy exists at {input_subdir}. Re-upload the files."
                ),
            )
        upload_folder.mkdir(parents=True, exist_ok=False)
        for f in input_subdir.iterdir():
            if f.is_file():
                shutil.copy2(f, upload_folder / f.name)

    # If a prior canceled job ran on this artifact, refuse to skip past the
    # step that was interrupted (its output dir is half-written; previous
    # steps' outputs are intact).
    clamp = store.cancel_clamp_for_artifact(art)
    if clamp is not None and from_step > clamp:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Cannot resume from step {from_step}: a prior canceled job "
                f"interrupted step {clamp}. Resume must re-run from step {clamp} "
                f"or earlier."
            ),
        )

    opts = GradeFormOpts(
        prompt=prompt,
        from_step=from_step,
        resume_dir=art,
        use_cache=use_cache,
    )
    _validate_opts(opts)

    job = store.create()
    create_background_task(_run_grade_job(job.id, upload_folder, opts))
    return {"id": job.id}


# ---------------------------------------------------------------------------
# Download endpoints — resolve via JobRecord.artifact_dir
# ---------------------------------------------------------------------------

def _require_artifact_dir(job_id: str) -> Path:
    rec = store.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if rec.artifact_dir is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Artifact directory not yet known — pipeline likely failed before "
                "step 2 (locate_exam_folder). Check the job log."
            ),
        )
    return rec.artifact_dir


def _file_or_404(path: Path, what: str, job_id: str) -> FileResponse:
    if not path.is_file():
        rec = store.get(job_id)
        failed_at = ""
        if rec is not None:
            for s in rec.steps:
                if s.status == JobStatus.FAILED:
                    failed_at = f" (pipeline failed at step {s.num} {s.name!r})"
                    break
        raise HTTPException(
            status_code=404,
            detail=f"{what} not produced{failed_at}",
        )
    return FileResponse(path, filename=path.name)


@router.get("/api/grade/jobs/{job_id}/student-pdfs.zip")
async def download_student_pdfs_zip(job_id: str) -> Response:
    from xscore.shared.exam_paths import artifact_student_pdfs_dir
    art = _require_artifact_dir(job_id)
    pdf_dir = artifact_student_pdfs_dir(art)
    pdfs: list[Path] = sorted(pdf_dir.glob("*.pdf")) if pdf_dir.is_dir() else []
    if not pdfs:
        rec = store.get(job_id)
        failed_at = ""
        if rec is not None:
            for s in rec.steps:
                if s.status == JobStatus.FAILED:
                    failed_at = f" (pipeline failed at step {s.num} {s.name!r})"
                    break
        raise HTTPException(status_code=404, detail=f"No per-student PDFs produced{failed_at}")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as zf:
        for p in pdfs:
            zf.write(p, arcname=p.name)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="student_pdfs_{job_id[:8]}.zip"'},
    )


@router.get("/api/grade/jobs/{job_id}/class-report.pdf")
async def download_class_report(job_id: str) -> FileResponse:
    from xscore.shared.exam_paths import artifact_class_report_pdf_path
    art = _require_artifact_dir(job_id)
    return _file_or_404(artifact_class_report_pdf_path(art), "Class report", job_id)


@router.get("/api/grade/jobs/{job_id}/review-queue")
async def download_review_queue(job_id: str) -> Response:
    from xscore.shared.exam_paths import (
        artifact_review_queue_json_path,
        artifact_review_queue_md_path,
        artifact_review_queue_txt_path,
    )
    art = _require_artifact_dir(job_id)
    candidates = [
        artifact_review_queue_json_path(art),
        artifact_review_queue_md_path(art),
        artifact_review_queue_txt_path(art),
    ]
    existing = [p for p in candidates if p.is_file()]
    if not existing:
        raise HTTPException(status_code=404, detail="Review queue not produced")
    if len(existing) == 1:
        return FileResponse(existing[0], filename=existing[0].name)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in existing:
            zf.write(p, arcname=p.name)
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="review_queue_{job_id[:8]}.zip"'},
    )


@router.get("/api/grade/jobs/{job_id}/cost.json")
async def download_cost(job_id: str) -> FileResponse:
    from xscore.shared.exam_paths import artifact_cost_json_path
    art = _require_artifact_dir(job_id)
    return _file_or_404(artifact_cost_json_path(art), "Cost summary", job_id)


@router.get("/api/grade/jobs/{job_id}/run-log.jsonl")
async def download_run_log(job_id: str) -> FileResponse:
    art = _require_artifact_dir(job_id)
    return _file_or_404(art / "run.log.jsonl", "Run log", job_id)


# ---------------------------------------------------------------------------
# Polling shim — surface artifact_dir + availability flags so the front-end can
# decide which download buttons to render. Adds to (does not replace) the
# generic /api/jobs/{id} response in nl_jobs.py.
# ---------------------------------------------------------------------------

@router.get("/api/grade/jobs/{job_id}/artifacts")
async def list_artifacts(job_id: str) -> JSONResponse:
    """Return availability flags for grade-only download artifacts."""
    rec = store.get(job_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="Job not found")
    out: dict = {
        "artifact_dir": str(rec.artifact_dir) if rec.artifact_dir else None,
        "upload_folder": str(rec.upload_folder) if rec.upload_folder else None,
    }
    if rec.artifact_dir is not None:
        from xscore.shared.exam_paths import (
            artifact_class_report_pdf_path,
            artifact_cost_json_path,
            artifact_review_queue_json_path,
            artifact_review_queue_md_path,
            artifact_review_queue_txt_path,
            artifact_student_pdfs_dir,
        )
        art = rec.artifact_dir
        student_pdfs_dir = artifact_student_pdfs_dir(art)
        has_student_pdfs = (
            student_pdfs_dir.is_dir() and any(student_pdfs_dir.glob("*.pdf"))
        )
        out["available"] = {
            "student_pdfs_zip": has_student_pdfs,
            "class_report_pdf": artifact_class_report_pdf_path(art).is_file(),
            "review_queue": any(
                p.is_file() for p in (
                    artifact_review_queue_json_path(art),
                    artifact_review_queue_md_path(art),
                    artifact_review_queue_txt_path(art),
                )
            ),
            "cost_json": artifact_cost_json_path(art).is_file(),
            "run_log_jsonl": (art / "run.log.jsonl").is_file(),
        }
        # Suggest the next resumable step if the job failed.
        if rec.status == JobStatus.FAILED:
            from xscore.shared.pipeline_steps import resumable_step_numbers
            failed_step_n = None
            for s in rec.steps:
                if s.status == JobStatus.FAILED:
                    failed_step_n = s.num
                    break
            resumable = resumable_step_numbers()
            target = next(
                (n for n in resumable if failed_step_n is not None and n >= failed_step_n),
                None,
            )
            out["resume_suggestion"] = {
                "from_step": target,
                "failed_step": failed_step_n,
                "resumable_steps": list(resumable),
            }
    return JSONResponse(content=out, headers={"Cache-Control": "no-store"})
