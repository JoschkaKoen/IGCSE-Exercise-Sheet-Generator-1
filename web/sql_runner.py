# -*- coding: utf-8 -*-
"""In-process SQL execution + result rendering, shared with the lesson validator.

The SQL analog of ``web.java_runner`` / ``web.c_runner``: the single source of
truth for how a SQL task is executed and how its result grid is rendered to text,
so the **browser** (``web/static/js/code-worker-sql.js``, running sql.js) and the
**lesson validator** (``scripts/check_code_lessons.py``, running this module) agree
— *validator-pass ⇒ runtime-pass*.

That guarantee holds because **sql.js is SQLite-in-WASM and this module uses
Python's stdlib ``sqlite3`` — the same engine**. The only place the two could
diverge is in turning a result set into the canonical text we string-compare, so
``render_grid`` here is kept **byte-identical** to ``renderGrid`` in the worker.
The one real hazard is number formatting (Python ``str(2.0)`` == ``"2.0"`` but JS
``String(2.0)`` == ``"2"``, and Python's ``.Nf`` rounds half-to-even while JS
``toFixed`` rounds half-up). ``_fmt`` normalises both: integral floats render with
no decimal, and non-integral floats are formatted to ``_DP`` places. **Authoring
convention:** wrap averages/divisions in ``ROUND(expr, 2)`` so values carry ≤2
decimals and ``_DP``-place formatting is exact on both engines (no rounding
boundary to disagree on).

Unlike the Java/C runners there is **no ``sandbox_exec`` import** — nothing spawns
a process; execution is in-process ``sqlite3`` against a throwaway in-memory DB.
Deliberately stdlib-only and FastAPI-free so the CLI validator can import it
without the web stack. There is no server endpoint today (SQL runs entirely
client-side); this module exists for the validator and as a ready core should a
``/api/code/run-sql`` ever be wanted.
"""

from __future__ import annotations

import sqlite3
from typing import Any

__all__ = ["run_sql", "render_grid", "compare_rows"]

# Canonical render constants — MUST match code-worker-sql.js exactly.
_SEP = " | "      # cell separator (avoid embedding it in seed data)
_NULL = "NULL"    # how a SQL NULL renders
_DP = 6           # decimal places for non-integral floats


def _fmt(value: Any) -> str:
    """Render one cell to its canonical string. Mirrors ``fmtCell`` in the worker."""
    if value is None:
        return _NULL
    if isinstance(value, bool):  # defensive — SQLite has no bool, stores 0/1
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))          # 2.0 -> "2"
        return f"{value:.{_DP}f}".rstrip("0").rstrip(".")
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def run_sql(
    seed: str,
    code: str,
    probe: str | None = None,
) -> tuple[list[str], list[list[Any]], str | None]:
    """Run ``code`` against a fresh in-memory DB seeded with ``seed``; return
    ``(columns, rows, error)``. ``error`` is a message string on any SQL failure
    (never raises), in which case columns/rows are empty.

    Execution semantics — identical in the worker:
      - **with ``probe``**: run ``seed``, run ``code`` (mutations: INSERT/UPDATE/
        DELETE/CREATE — may be several statements), then run the single ``probe``
        SELECT and return its grid.
      - **without ``probe``**: ``code`` is a single query; return its grid.
    """
    con = sqlite3.connect(":memory:")
    try:
        cur = con.cursor()
        if seed:
            cur.executescript(seed)
        if probe:
            if code and code.strip():
                cur.executescript(code)     # mutations (one or more statements)
            cur.execute(probe)              # single SELECT reads the resulting state
        else:
            cur.execute(code)               # single query
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchall()] if cur.description else []
        return cols, rows, None
    except Exception as exc:                # noqa: BLE001 — any SQL error is a failure, surfaced as text
        return [], [], str(exc)
    finally:
        con.close()


def render_grid(columns: list[str], rows: list[list[Any]], ordered: bool = True) -> str:
    """Canonical text grid: a header line of column names, then one line per row,
    cells joined by ``_SEP``. With ``ordered=False`` the rendered row lines are
    sorted (set semantics for queries without ``ORDER BY``). MUST stay
    byte-identical to ``renderGrid`` in ``code-worker-sql.js``."""
    header = _SEP.join(str(c) for c in columns)
    body = [_SEP.join(_fmt(c) for c in row) for row in rows]
    if not ordered:
        body = sorted(body)
    return "\n".join([header] + body)


def compare_rows(got_text: str, expected_text: Any) -> bool:
    """True iff two rendered grids match after stripping surrounding whitespace —
    the same trim-compare the worker (and ``compare_stdout``) use."""
    return (got_text or "").strip() == str(expected_text if expected_text is not None else "").strip()
