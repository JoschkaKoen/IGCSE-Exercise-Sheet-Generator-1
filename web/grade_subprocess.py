# -*- coding: utf-8 -*-
"""Subprocess-based grade pipeline runner.

Spawns ``XScore.py`` as a child process with its own stdout, captures both
human-readable logs and structured per-step events through that single channel,
and exposes an immediate SIGTERM cancel. Replaces the in-process runner that
had to swap ``sys.stdout`` globally (which broke concurrency between jobs).

Channel design: stdout carries both. Lines prefixed with ``EVENT_SENTINEL`` are
JSON event payloads (routed to ``on_event``); everything else is human log
output (routed to ``on_line``). The child emits events when started with
``XSCORE_EVENTS_TO_STDOUT=1`` in its env — see ``XScore.main``.

Process group: spawned with ``start_new_session=True`` so the child is the
leader of its own process group. Cancel sends SIGTERM to the *group*, reaching
every descendant (pdflatex, pdftoppm, etc.) — not just the Python parent.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from typing import Callable

from .grade_service import GradeFormOpts

PROJECT_ROOT = Path(__file__).resolve().parent.parent
EVENT_SENTINEL = "__XSCORE_EVENT__\t"

_CACHE_PHRASES = ("use cache", "reuse cache", "from cache", "cache reuse")


def _effective_prompt(opts: GradeFormOpts) -> str:
    """Apply the use_cache convenience: prepend the cache opt-in phrase if absent."""
    prompt = (opts.prompt or "").strip()
    if opts.use_cache and not any(p in prompt.lower() for p in _CACHE_PHRASES):
        prompt = ("use cache " + prompt).strip()
    return prompt


def build_argv(folder: Path, opts: GradeFormOpts) -> list[str]:
    """Translate a ``GradeFormOpts`` into an ``XScore.py`` command line.

    Mirrors the CLI surface in ``XScore.parse_args``. ``--folder`` is always
    passed so the natural-language exam-folder resolver is bypassed (web users
    always upload; they never name an exam by prompt).
    """
    argv: list[str] = [sys.executable, "XScore.py", _effective_prompt(opts), "--folder", str(folder)]
    if opts.force_clean_scan:
        argv.append("--force-clean-scan")
    if opts.stop_after is not None:
        argv += ["--stop-after", str(opts.stop_after)]
    if opts.from_step is not None:
        argv += ["--from-step", str(opts.from_step)]
    if opts.resume_dir is not None:
        argv += ["--resume-dir", str(opts.resume_dir)]
    if opts.students:
        for name in opts.students:
            argv += ["--student", name]
    if opts.limit_students is not None:
        argv += ["--limit-students", str(opts.limit_students)]
    return argv


async def run_grade_subprocess(
    folder: Path,
    opts: GradeFormOpts,
    *,
    on_line: Callable[[str], None],
    on_event: Callable[[dict], None],
    register_proc: Callable[[asyncio.subprocess.Process], None],
) -> int:
    """Spawn XScore.py, route stdout to *on_line*/*on_event*, return exit code.

    *register_proc* is invoked synchronously immediately after spawn so the
    cancel endpoint can reach the process before any output is consumed.
    """
    env = {
        **os.environ,
        "XSCORE_EVENTS_TO_STDOUT": "1",
        "PYTHONUNBUFFERED": "1",
    }
    argv = build_argv(folder, opts)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    register_proc(proc)
    assert proc.stdout is not None
    while True:
        line_bytes = await proc.stdout.readline()
        if not line_bytes:
            break
        line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
        if line.startswith(EVENT_SENTINEL):
            payload = line[len(EVENT_SENTINEL):]
            try:
                evt = json.loads(payload)
            except json.JSONDecodeError:
                # Malformed event line — fall through to plain-log handling so
                # we never lose output (better visible garbage than dropped).
                try:
                    on_line(line)
                except Exception:
                    pass
                continue
            try:
                on_event(evt)
            except Exception:
                pass  # observer faults must not kill the pump
        else:
            try:
                on_line(line)
            except Exception:
                pass
    return await proc.wait()


async def cancel_process(proc: asyncio.subprocess.Process) -> None:
    """SIGTERM the child's process group; escalate to SIGKILL after 3 s.

    Idempotent and safe on already-exited processes. Relies on the child being
    spawned with ``start_new_session=True`` so ``proc.pid`` is the PGID.
    """
    pid = proc.pid
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return
    try:
        await proc.wait()
    except Exception:
        pass
