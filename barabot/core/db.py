"""SQLite schema, migrations, and connection setup."""
from __future__ import annotations

import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    msg_id      INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    embedding   BLOB    NOT NULL,
    sender      TEXT,
    created_at  TEXT    NOT NULL,
    photo_count INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chat_id, msg_id)
);

-- inbox_queue: scraper → processor. Raw chat messages awaiting processing.
CREATE TABLE IF NOT EXISTS inbox_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    chat_title TEXT,
    sender     TEXT,
    text       TEXT    NOT NULL,
    enqueued_at TEXT   NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending',
    claimed_at TEXT,
    claimed_by TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT,
    UNIQUE(chat_id, msg_id)
);

-- outbox_queue: any service → bot. Pre-rendered messages ready for delivery.
CREATE TABLE IF NOT EXISTS outbox_queue (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    text           TEXT    NOT NULL,
    reply_to_msg_id INTEGER,
    enqueued_at    TEXT    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'pending',
    claimed_at     TEXT,
    claimed_by     TEXT,
    attempts       INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);

-- command_queue: incoming user-facing commands (audit log + replay).
CREATE TABLE IF NOT EXISTS command_queue (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    source     TEXT    NOT NULL,
    command    TEXT    NOT NULL,
    args       TEXT,
    enqueued_at TEXT   NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'pending',
    claimed_at TEXT,
    claimed_by TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_inbox_pending
    ON inbox_queue(status, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox_queue(status, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_command_pending
    ON command_queue(status, id) WHERE status = 'pending';
"""


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent column-level migrations for already-deployed databases.

    `CREATE TABLE IF NOT EXISTS` won't add a column to a pre-existing table,
    so we check `PRAGMA table_info` and run `ALTER TABLE ... ADD COLUMN` only
    for columns that don't exist yet. Safe to call on every connect().
    """
    existing = {
        row[1] for row in conn.execute("PRAGMA table_info(items)").fetchall()
    }
    if "photo_count" not in existing:
        # Backfill row gets DEFAULT 1, meaning "we don't know if it was an
        # album; treat as single-photo". New writes carry the real value.
        conn.execute("ALTER TABLE items ADD COLUMN photo_count INTEGER NOT NULL DEFAULT 1")

def connect(path: Path) -> sqlite3.Connection:
    """Open the DB, ensure schema, turn on WAL. Idempotent — safe to call repeatedly."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.executescript(SCHEMA)
    _apply_migrations(conn)
    # WAL: concurrent readers don't block the writer, and vice versa.
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL is the standard recommendation with WAL: crash-safe without the
    # fsync-per-commit cost of FULL.
    conn.execute("PRAGMA synchronous=NORMAL")
    # Wait up to 5s when another connection holds the write lock instead
    # of failing with SQLITE_BUSY immediately. Matters for concurrent
    # claim/ack from multiple services sharing one SQLite file.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn
