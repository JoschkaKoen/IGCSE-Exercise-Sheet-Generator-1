# -*- coding: utf-8 -*-
"""Capture stdout/stderr into a streaming “current line” for the web UI."""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


def run_with_last_log_line(
    fn: Callable[[], T],
    on_line: Callable[[str], None],
    *,
    max_line_len: int = 600,
) -> T:
    """
    Run ``fn`` with stdout and stderr redirected; invoke ``on_line`` with the
    text that would appear on the terminal’s current line (last completed line,
    or the in-progress line if there is no trailing newline yet).
    """
    remainder: str = ""

    def trunc(t: str) -> str:
        t = t.strip()
        if len(t) > max_line_len:
            return t[: max_line_len - 1] + "…"
        return t

    def feed(s: str) -> None:
        nonlocal remainder
        if not s:
            return
        if not isinstance(s, str):
            s = str(s)
        remainder += s
        while "\n" in remainder:
            line, remainder = remainder.split("\n", 1)
            if line.strip():
                on_line(trunc(line))
        if remainder.strip():
            on_line(trunc(remainder))

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

    cap = _StdCapture()
    old_out, old_err = sys.stdout, sys.stderr
    try:
        sys.stdout = sys.stderr = cap  # type: ignore[assignment]
        return fn()
    finally:
        sys.stdout, sys.stderr = old_out, old_err
