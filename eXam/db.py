"""SQLite schema + connection helper for the eXam pipeline.

Single DB at ``output/eXam/eXam.db``; WAL mode for concurrent reads alongside
build-thread writes. Tables: students, tests, attempts, question_helpers
(see plan for the schema).
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from eXercise.config import PROJECT_ROOT

DB_PATH = PROJECT_ROOT / "output" / "eXam" / "eXam.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS students (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  pin TEXT NOT NULL,
  class_label TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tests (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  teacher_prompt TEXT NOT NULL,
  subject TEXT NOT NULL,
  class_label TEXT,
  question_ids TEXT NOT NULL,
  randomize INTEGER NOT NULL,
  status TEXT NOT NULL,
  build_progress TEXT,
  build_error TEXT,
  created_at TEXT NOT NULL,
  ready_at TEXT
);

CREATE TABLE IF NOT EXISTS attempts (
  id INTEGER PRIMARY KEY,
  student_id INTEGER NOT NULL REFERENCES students(id),
  test_id TEXT NOT NULL REFERENCES tests(id),
  question_id TEXT NOT NULL,
  attempt_number INTEGER NOT NULL,
  submitted TEXT NOT NULL,
  assigned_marks REAL NOT NULL,
  max_marks REAL NOT NULL,
  reasoning TEXT,
  hint_used INTEGER NOT NULL DEFAULT 0,
  solution_revealed INTEGER NOT NULL DEFAULT 0,
  example_used INTEGER NOT NULL DEFAULT 0,
  kb_used INTEGER NOT NULL DEFAULT 0,
  submitted_at TEXT NOT NULL,
  UNIQUE (student_id, test_id, question_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS attempts_latest
  ON attempts (student_id, test_id, question_id, attempt_number DESC);

CREATE TABLE IF NOT EXISTS question_helpers (
  question_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  generated_with TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (question_id, kind)
);
"""

_init_lock = threading.Lock()
_initialised = False


def _init_db() -> None:
    """Create the DB file and tables if missing; enable WAL."""
    global _initialised
    if _initialised:
        return
    with _init_lock:
        if _initialised:
            return
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        _initialised = True


@contextmanager
def connect():
    """Yield a sqlite3 connection (row factory = sqlite3.Row). Caller commits."""
    _init_db()
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()
