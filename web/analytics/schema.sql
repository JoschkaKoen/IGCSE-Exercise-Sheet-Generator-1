-- web/analytics/schema.sql
-- Idempotent DDL for the analytics SQLite database. Run by store.init_db()
-- on every startup; safe to re-run.
--
-- Bump user_version + add ALTER TABLE blocks (guarded by version checks
-- in store.init_db) for future migrations.

PRAGMA journal_mode = WAL;
PRAGMA user_version = 1;

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    kind            TEXT    NOT NULL,
    method          TEXT,
    session_id      TEXT,
    route           TEXT,
    referrer        TEXT,
    status          TEXT,
    user_kind       TEXT,
    properties_json TEXT,
    duration_ms     INTEGER
);

CREATE INDEX IF NOT EXISTS events_ts        ON events(ts);
CREATE INDEX IF NOT EXISTS events_kind_ts   ON events(kind, ts);
CREATE INDEX IF NOT EXISTS events_session   ON events(session_id);

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    user_kind   TEXT,
    ua_browser  TEXT,
    ua_os       TEXT,
    is_mobile   INTEGER,
    ip_hash     TEXT
);

CREATE INDEX IF NOT EXISTS sessions_last_seen ON sessions(last_seen);
