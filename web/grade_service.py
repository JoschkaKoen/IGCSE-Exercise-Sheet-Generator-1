# -*- coding: utf-8 -*-
"""Thin adapter: drive the canonical xScore pipeline from the web grade page.

Builds an ``argparse.Namespace`` from form fields, captures stdout for the
human-readable scrollback, and dispatches ``xscore.pipeline.runner.run_pipeline``
with a step-event observer. All 37 pipeline steps + resume support come for free
because we use the same orchestrator the CLI uses.

The ``GradeFormOpts`` dataclass mirrors the CLI flags exposed by ``XScore.py``
(``--dpi``, ``--force-clean-scan``, ``--stop-after``, ``--from-step``,
``--resume-dir``, ``--student``, ``--limit-students``) plus a single web-only
convenience: ``use_cache`` (which prepends ``"use cache "`` to the prompt; the
canonical opt-in mechanism is the prompt-phrase heuristic in
``xscore/marking/parse_instruction.py`` — there is no CLI flag for it).
"""

from __future__ import annotations

import argparse
import datetime
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from xscore.pipeline.runner import run_pipeline
from xscore.shared.exam_paths import DESKEW_DIR


@dataclass
class GradeFormOpts:
    """Form fields for a grade-pipeline submission, mirroring XScore.py CLI flags."""

    prompt: str | None = None
    dpi: int | None = None
    force_clean_scan: bool = False
    stop_after: int | None = None
    from_step: int | None = None
    resume_dir: Path | None = None
    students: list[str] | None = None
    limit_students: int | None = None
    use_cache: bool = False  # prepends "use cache " to prompt server-side


def _effective_prompt(opts: GradeFormOpts) -> str:
    """Apply the use_cache convenience: prepend the cache opt-in phrase if needed."""
    prompt = (opts.prompt or "").strip()
    if opts.use_cache:
        # Idempotent: only prepend if the substring isn't already in the prompt.
        cache_phrases = ("use cache", "reuse cache", "from cache", "cache reuse")
        if not any(p in prompt.lower() for p in cache_phrases):
            prompt = ("use cache " + prompt).strip()
    return prompt


def run_xscore_pipeline(
    folder: Path,
    opts: GradeFormOpts,
    on_step_event: Callable[[dict], None],
    stdout_tee_factory: Callable[[], object] | None = None,
) -> tuple[Path | None, Path | None]:
    """Build args + dispatch the canonical run_pipeline; return (cleaned_pdf, artifact_dir).

    *folder* is the upload folder (already populated with ``scan*.pdf``,
    ``StudentList.*``, ``empty_exam.pdf``, ``answer_sheet.pdf``).

    *on_step_event* is invoked for every step transition with a dict
    (``step_number``, ``step_name``, ``status``, ``duration_s``, ``artifact_dir``,
    ``error``). The web layer translates these into JobStore updates.

    *stdout_tee_factory*, when provided, returns an object that replaces
    ``sys.stdout`` for the duration of the run (intended for the web's
    ``_StdoutTee`` which captures the human-readable scrollback). The tee MUST
    expose a ``_log = True`` attribute so ``xscore.marking.ai_mark`` disables
    its Rich Live in-place updates (otherwise they clobber the captured stream).

    Returns:
        ``(cleaned_pdf, artifact_dir)``. Either may be ``None`` if the pipeline
        failed before the corresponding artifact was produced.
    """
    # _Ctx.__post_init__ at pipeline_ctx.py:103 only overrides its dataclass
    # defaults when args.* is not None. So None is the correct sentinel for
    # stop_after / from_step / resume_dir / student / limit_students.
    # force_clean_scan must default to False (bool, not None — checked truthily
    # at prelude.py:44 via `args.force_clean_scan or inst.force_clean_scan`).
    args = argparse.Namespace(
        prompt=_effective_prompt(opts),
        folder=folder,                       # CLI override → bypasses NL folder resolve
        dpi=opts.dpi,                        # int | None
        force_clean_scan=opts.force_clean_scan,  # bool
        stop_after=opts.stop_after,          # int | None
        from_step=opts.from_step,            # int | None
        resume_dir=opts.resume_dir,          # Path | None
        student=opts.students,               # list[str] | None
        limit_students=opts.limit_students,  # int | None
    )

    timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    artifact_dir_holder: dict[str, Path] = {}

    def _wrapped_event(evt: dict) -> None:
        if evt.get("artifact_dir"):
            artifact_dir_holder["dir"] = Path(evt["artifact_dir"])
        on_step_event(evt)

    real_stdout = sys.stdout
    tee = stdout_tee_factory() if stdout_tee_factory else None
    if tee is not None:
        sys.stdout = tee  # type: ignore[assignment]
    try:
        run_pipeline(args, timestamp, on_step_event=_wrapped_event)
    finally:
        if tee is not None:
            try:
                flush = getattr(tee, "flush", None)
                if callable(flush):
                    flush()
            finally:
                sys.stdout = real_stdout

    artifact_dir = artifact_dir_holder.get("dir")
    cleaned: Path | None = None
    if artifact_dir:
        # Mirror the fallback chain in xscore/pipeline/resume.py:198-203.
        for candidate in (
            artifact_dir / DESKEW_DIR / "cleaned_scan.pdf",
            artifact_dir / "cleaned_scan.pdf",
        ):
            if candidate.is_file():
                cleaned = candidate
                break
    return cleaned, artifact_dir
