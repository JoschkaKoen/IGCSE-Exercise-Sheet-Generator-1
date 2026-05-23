"""SQLite schema + connection helper for the eXam pipeline.

Single DB at ``output/eXam/eXam.db``; WAL mode for concurrent reads alongside
build-thread writes. Tables: students, tests, attempts, question_helpers,
open_sessions / open_views / open_attempts, ai_calls, users.

Schema versions are tracked via ``PRAGMA user_version``. ``_SCHEMA`` is the
v0 baseline (kept as a single CREATE TABLE IF NOT EXISTS block for new
deployments); ``_MIGRATIONS`` is an append-only list of upgrade scripts
applied in order. On every startup we run any migrations whose target
version exceeds the current ``user_version`` — idempotent for both fresh
installs and in-place upgrades.
"""

from __future__ import annotations

import datetime as _dt
import os
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

-- Helpers (hint / solution / example / kb) are cached on disk under
-- output/eXam/bank/<subject>/<paper>/<qnum>/helpers/<kind>.md, not in SQLite.
-- The legacy question_helpers table is intentionally not created here;
-- any pre-existing copy on already-migrated DBs sits dormant and harmless.

-- Open-mode (anonymous, public-website practice) — see eXam/open_mode.py.
CREATE TABLE IF NOT EXISTS open_sessions (
  id TEXT PRIMARY KEY,
  created_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS open_attempts (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES open_sessions(id),
  question_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  submitted TEXT NOT NULL,
  assigned_marks REAL NOT NULL,
  max_marks REAL NOT NULL,
  reasoning TEXT,
  submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS open_attempts_by_session
  ON open_attempts (session_id, submitted_at DESC);

-- Records every question shown to a session, distinct per (session, question).
-- Drives both the "viewed" counter and the picker's exclude set so the same
-- question is not served twice in one session until the pool is exhausted.
CREATE TABLE IF NOT EXISTS open_views (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES open_sessions(id) ON DELETE CASCADE,
  question_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  viewed_at TEXT NOT NULL,
  UNIQUE (session_id, question_id)
);

CREATE INDEX IF NOT EXISTS open_views_by_session
  ON open_views (session_id, subject);

-- AI cost log: one row per successful API call across build / marking / helpers.
-- Populated by eXam/cost_tracker.py's DB sink (observer on the ai_client hook).
CREATE TABLE IF NOT EXISTS ai_calls (
  id INTEGER PRIMARY KEY,
  called_at TEXT NOT NULL,
  operation TEXT NOT NULL,
  test_id TEXT,
  student_id INTEGER,
  question_id TEXT,
  model TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  thinking_tokens INTEGER NOT NULL DEFAULT 0,
  duration_s REAL NOT NULL,
  cost_rmb REAL
);

CREATE INDEX IF NOT EXISTS ai_calls_by_test ON ai_calls (test_id, called_at);
CREATE INDEX IF NOT EXISTS ai_calls_by_op   ON ai_calls (operation, called_at);
"""


# Append-only list of migration scripts. Index N upgrades the DB from
# user_version=N to user_version=N+1. Never reorder, never edit a past entry —
# add new ones at the end.
_MIGRATIONS: list[str] = [
    # 0 → 1: user-management foundation.
    # - new ``users`` table for self-signed-up accounts (separate from class-mode
    #   ``students``);
    # - nullable ``user_id`` on session / view / attempt / cost / test rows so
    #   logged-in actions can be attributed cross-device;
    # - ``pipeline`` tag on ``ai_calls`` so the dashboard can split spend across
    #   eXercise / eXam / xScore (historical rows backfilled to ``'exam'``).
    """
    CREATE TABLE IF NOT EXISTS users (
      id INTEGER PRIMARY KEY,
      username TEXT NOT NULL,
      username_key TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'student',
      created_at TEXT NOT NULL,
      last_login_at TEXT
    );

    ALTER TABLE open_sessions ADD COLUMN user_id INTEGER REFERENCES users(id);
    ALTER TABLE open_views    ADD COLUMN user_id INTEGER REFERENCES users(id);
    ALTER TABLE open_attempts ADD COLUMN user_id INTEGER REFERENCES users(id);
    ALTER TABLE ai_calls      ADD COLUMN user_id INTEGER REFERENCES users(id);
    ALTER TABLE tests         ADD COLUMN user_id INTEGER REFERENCES users(id);

    ALTER TABLE ai_calls ADD COLUMN pipeline TEXT;
    UPDATE ai_calls SET pipeline = 'exam' WHERE pipeline IS NULL;

    CREATE INDEX IF NOT EXISTS ai_calls_by_user      ON ai_calls (user_id, called_at);
    CREATE INDEX IF NOT EXISTS ai_calls_by_pipeline  ON ai_calls (pipeline, called_at);
    CREATE INDEX IF NOT EXISTS open_views_by_user    ON open_views (user_id, subject);
    CREATE INDEX IF NOT EXISTS open_attempts_by_user ON open_attempts (user_id, submitted_at DESC);
    """,
]


_init_lock = threading.Lock()
_initialised = False


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Apply any pending migrations. Versioned via ``PRAGMA user_version``."""
    version = int(conn.execute("PRAGMA user_version").fetchone()[0] or 0)
    while version < len(_MIGRATIONS):
        conn.executescript(_MIGRATIONS[version])
        version += 1
        conn.execute(f"PRAGMA user_version = {version}")


def _bootstrap_admin(conn: sqlite3.Connection) -> None:
    """Idempotently seed the admin row from env (``BOOTSTRAP_ADMIN_USERNAME`` /
    ``BOOTSTRAP_ADMIN_PASSWORD``). No-op if either env is empty.

    When bootstrap *is* attempted, ``APP_SECRET_KEY`` must be set or admin
    cookies would be trivially forgeable against a known dev-fallback key
    (see ``eXam/auth.py:23``). Raises ``RuntimeError`` in that case rather
    than silently writing a forgeable admin.
    """
    username = (os.environ.get("BOOTSTRAP_ADMIN_USERNAME") or "").strip()
    password = (os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or "").strip()
    if not (username and password):
        return
    if not (os.environ.get("APP_SECRET_KEY") or "").strip():
        raise RuntimeError(
            "BOOTSTRAP_ADMIN_USERNAME / BOOTSTRAP_ADMIN_PASSWORD is set but "
            "APP_SECRET_KEY is not. Set APP_SECRET_KEY in .env first — "
            "without it, admin auth cookies are forgeable against the "
            "dev-fallback signing key."
        )
    # Local import to keep db.py importable from contexts where eXam.users
    # might be patched out (it's a pure-Python module — no real risk, just
    # keeps the import surface of db.py minimal).
    from eXam.users import hash_password, normalize_username_key

    key = normalize_username_key(username)
    row = conn.execute("SELECT id FROM users WHERE username_key = ?", (key,)).fetchone()
    if row is not None:
        return  # already bootstrapped — idempotent
    conn.execute(
        "INSERT INTO users (username, username_key, password_hash, role, created_at) "
        "VALUES (?, ?, ?, 'admin', ?)",
        (username, key, hash_password(password), _dt.datetime.now(_dt.UTC).isoformat()),
    )


def _init_db() -> None:
    """Create the DB file and tables if missing; enable WAL; run pending
    migrations; bootstrap the admin row when env asks for it."""
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
            _run_migrations(conn)
            _bootstrap_admin(conn)
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
