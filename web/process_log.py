# -*- coding: utf-8 -*-
"""Capture stdout/stderr into a streaming "current line" for the web UI."""

from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")

_tls = threading.local()
_install_lock = threading.Lock()
_installed = False


class _DispatchStream:
    """Single global stdout/stderr replacement that routes writes per-thread."""

    def __init__(self, real: object) -> None:
        self._real = real
        self.encoding: str = getattr(real, "encoding", "utf-8")

    def write(self, s: str) -> int:
        cap = getattr(_tls, "capture", None)
        if cap is not None:
            return cap.write(s)
        return self._real.write(s)  # type: ignore[union-attr]

    def flush(self) -> None:
        cap = getattr(_tls, "capture", None)
        if cap is not None:
            cap.flush()
        else:
            self._real.flush()  # type: ignore[union-attr]

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


def _install_dispatch() -> None:
    global _installed
    if _installed:
        return
    with _install_lock:
        if not _installed:
            sys.stdout = _DispatchStream(sys.stdout)  # type: ignore[assignment]
            sys.stderr = _DispatchStream(sys.stderr)  # type: ignore[assignment]
            _installed = True


def run_with_last_log_line(
    fn: Callable[[], T],
    on_line: Callable[[str], None],
    *,
    max_line_len: int = 700,
) -> T:
    """
    Run ``fn`` with stdout and stderr redirected; invoke ``on_line`` with the
    text that would appear on the terminal's current line (last completed line,
    or the in-progress line if there is no trailing newline yet).
    """
    remainder: str = ""

    def trunc(t: str, *, tail: bool = False) -> str:
        t = t.strip()
        if len(t) > max_line_len:
            if tail:
                return "…" + t[-(max_line_len - 1):]
            return t[: max_line_len - 1] + "…"
        return t

    _REMAINDER_CAP = 4 * 1024 * 1024  # 4 MB — prevents OOM on no-newline output

    def feed(s: str) -> None:
        nonlocal remainder
        if not s:
            return
        if not isinstance(s, str):
            s = str(s)
        remainder += s
        if len(remainder) > _REMAINDER_CAP:
            remainder = remainder[-_REMAINDER_CAP:]
        while "\n" in remainder:
            line, remainder = remainder.split("\n", 1)
            if line.strip():
                on_line(trunc(line))
        if remainder.strip():
            # Show the latest text (tail) for the in-progress line so that
            # long streaming output (e.g. AI thinking tokens without newlines)
            # keeps updating instead of freezing at the first 600 chars.
            on_line(trunc(remainder, tail=True))

    class _StdCapture:
        encoding = "utf-8"

        def write(self, s: str) -> int:
            if not isinstance(s, str):
                s = str(s)
            feed(s)
            return len(s)

        def flush(self) -> None:
            return

        def isatty(self) -> bool:
            return False

        def writable(self) -> bool:
            return True

    _install_dispatch()
    cap = _StdCapture()
    _tls.capture = cap
    try:
        return fn()
    finally:
        _tls.capture = None
