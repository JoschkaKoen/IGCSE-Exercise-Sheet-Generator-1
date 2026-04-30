# -*- coding: utf-8 -*-
"""Grade pipeline jobs: upload exam scans, run xScore steps 1–14, return PDF."""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import sys
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from .._state import create_background_task, store
from ..grade_auth import require_grade_unlock
from ..grade_service import run_full_pipeline_logged
from ..jobs import JobStatus

router = APIRouter()


_GRADE_UPLOADS_ROOT = Path(__file__).resolve().parent.parent.parent / "output" / "xscore" / "grade_uploads"

# ANSI CSI + OSC strippers — mirrors xScore.py:_Tee for the captured-stream path.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07]*\x07")


class _StdoutTee:
    """Mirror writes to the original stdout AND emit completed lines to a callback.

    Carries a ``_log = True`` marker attribute so ``xscore/marking/ai_mark.py``
    auto-disables ``rich.live.Live`` (no in-place cursor updates we'd have to
    re-strip later).
    """

    _log = True  # marker for ai_mark.py's `not hasattr(sys.stdout, '_log')` check

    def __init__(self, real_stdout, on_line):
        self._real = real_stdout
        self._on_line = on_line
        self._buf = ""

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            text = str(text)
        try:
            n = self._real.write(text)
        except Exception:
            n = len(text)
        self._buf += text
        if "\n" in self._buf:
            parts = self._buf.split("\n")
            self._buf = parts[-1]
            for line in parts[:-1]:
                self._emit(line)
        return n if n is not None else len(text)

    def flush(self) -> None:
        try:
            self._real.flush()
        except Exception:
            pass
        if self._buf:
            self._emit(self._buf)
            self._buf = ""

    def _emit(self, line: str) -> None:
        clean = _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", line)).rstrip("\r")
        if clean.strip() or line.strip() == "":
            try:
                self._on_line(clean)
            except Exception:
                pass

    def isatty(self) -> bool:  # pragma: no cover — explicit so Rich sees non-TTY
        return False

    def __getattr__(self, name):
        # Forward anything else (e.g., .encoding) to the original stdout.
        return getattr(self._real, name)


def _run_with_capture(folder, prompt, on_line, on_step, on_capture):
    """Run the pipeline with stdout teed into ``on_capture`` for the duration.

    Single-user assumption: ``sys.stdout`` is module-level, so this is safe
    only because ``JobStore`` is documented single-user (see ``web/jobs.py``).
    Concurrent grade jobs would race on the global stdout slot.
    """
    real_stdout = sys.stdout
    tee = _StdoutTee(real_stdout, on_capture)
    sys.stdout = tee
    try:
        return run_full_pipeline_logged(folder, prompt, on_line, on_step)
    finally:
        try:
            tee.flush()
        finally:
            sys.stdout = real_stdout


_GRADE_STEPS = [
    (1,  "Parse grading instructions"),
    (2,  "Folder from upload"),
    (3,  "Load student roster"),
    (4,  "Detect blank pages"),
    (5,  "Autorotate"),
    (6,  "Deskew"),
    (7,  "Exam geometry + assign pages"),
    (8,  "Parse exam PDF"),
    (9,  "Parse mark scheme"),
    (10, "Build scaffold"),
    (11, "Build blueprints"),
    (12, "AI marking"),
    (13, "Compile reports"),
    (14, "Timing summary"),
]


async def _run_grade_job(
    job_id: str,
    folder: Path,
    prompt: str | None,
) -> None:
    store.set_status(job_id, JobStatus.RUNNING)
    store.init_steps(job_id, _GRADE_STEPS)
    store.set_log_line(job_id, "Starting pipeline…")

    def on_line(line: str) -> None:
        store.set_log_line(job_id, line)

    def on_step(num: int, event: str, elapsed_s: float | None) -> None:
        if event == "running":
            store.step_running(job_id, num)
        elif event == "done":
            store.step_done(job_id, num, elapsed_s or 0.0)
        elif event == "failed":
            store.step_failed(job_id, num, elapsed_s or 0.0)

    def on_capture(line: str) -> None:
        store.append_log_line(job_id, line)

    try:
        cleaned_pdf, artifact_dir = await asyncio.to_thread(
            _run_with_capture, folder, prompt, on_line, on_step, on_capture
        )
        from xscore.shared.exam_paths import artifact_class_report_pdf_path
        class_report_pdf = artifact_class_report_pdf_path(artifact_dir)
        store.complete(
            job_id,
            output_pdf=cleaned_pdf,
            answers_pdf=class_report_pdf if class_report_pdf.is_file() else None,
        )
    except Exception as e:  # noqa: BLE001
        logging.exception("Grade pipeline failed for job %s", job_id)
        store.fail(job_id, f"Pipeline error: {e}")


_SCAN_NUMBERED_RE = re.compile(r"^scan[\s_\-]*(\d+)\b", re.IGNORECASE)


@router.post("/api/grade/jobs")
async def create_grade_job(
    exam_scans: list[UploadFile] = File(...),
    student_list: UploadFile = File(...),
    empty_exam: UploadFile = File(...),
    answer_sheet: UploadFile = File(...),
    prompt: str | None = Form(None),
    _gate: None = Depends(require_grade_unlock),
) -> dict[str, str]:
    """Accept uploaded exam files, save them, and launch an xScore pipeline job.

    *exam_scans* accepts either a single PDF (saved as ``scan.pdf`` for the
    single-PDF / DPI-keyed pipeline branch) or multiple PDFs whose original
    filenames must each match ``scan{N}`` with optional separator (``scan1.pdf``,
    ``scan 2.pdf``, ``scan_3.pdf``, ``scan-4.pdf``, ...). Each numbered file is
    saved canonically as ``scan{N}.pdf``.
    """
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    # 8 random bytes = 2^64 space; retry on collision (astronomically rare)
    for _ in range(5):
        upload_id = f"{ts}_{os.urandom(8).hex()}"
        folder = _GRADE_UPLOADS_ROOT / upload_id
        if not folder.exists():
            folder.mkdir(parents=True, exist_ok=False)
            break
    else:
        raise RuntimeError("Could not create a unique upload directory")

    _MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB

    async def _read_limited(upload: UploadFile, name: str) -> bytes:
        # Early rejection via Content-Length header (avoids reading the body at all)
        cl = upload.headers.get("content-length")
        if cl and int(cl) > _MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"{name} exceeds the {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB upload limit",
            )
        # Chunked read: reject as soon as running total crosses the limit
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await upload.read(1024 * 1024)  # 1 MB chunks
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

    # Save required files — single PDF stays as scan.pdf (single-PDF mode);
    # multiple PDFs require numbered names and are saved canonically as scanN.pdf.
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
    sl_path = folder / f"StudentList{sl_suffix}"
    sl_path.write_bytes(await _read_limited(student_list, "student_list"))

    # Save required files (all four are now mandatory)
    (folder / "empty_exam.pdf").write_bytes(await _read_limited(empty_exam, "empty_exam"))
    (folder / "answer_sheet.pdf").write_bytes(await _read_limited(answer_sheet, "answer_sheet"))

    job = store.create()
    create_background_task(_run_grade_job(job.id, folder, prompt or None))
    return {"id": job.id}
