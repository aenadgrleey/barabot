"""Application database in data/barabot.sqlite.

DBOS keeps its own system database next to it. This one holds the
domain tables: `messages` written by the scraper, `listings` whose `rev`
the scraper bumps and whose content and `version` processing writes.
`subscriptions` and `deliveries` arrive with the bot slice.

A listing's identity is derived by the schema, not by Python. Each
message row carries generated `listing_album` and `listing_ref` columns:
an album groups by `grouped_id`, a single message stands alone by
`msg_id`. Together with `chat_id` they form the primary key of
`listings`. The album flag keeps grouped_id and msg_id values apart.

Every caller opens a short-lived connection, usually inside
`asyncio.to_thread`, so no connection crosses threads.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id       INTEGER NOT NULL,
    msg_id        INTEGER NOT NULL,
    grouped_id    INTEGER,
    sender_id     INTEGER,
    date          TEXT    NOT NULL,
    text          TEXT    NOT NULL DEFAULT '',
    media         TEXT    NOT NULL DEFAULT '[]',
    deleted       INTEGER NOT NULL DEFAULT 0,
    listing_album INTEGER GENERATED ALWAYS AS (grouped_id IS NOT NULL) VIRTUAL,
    listing_ref   INTEGER GENERATED ALWAYS AS (coalesce(grouped_id, msg_id)) VIRTUAL,
    PRIMARY KEY (chat_id, msg_id)
);
-- Deletes from basic groups and private chats arrive without chat_id.
CREATE INDEX IF NOT EXISTS messages_by_msg_id ON messages (msg_id);
-- Assembly reads a listing's messages in msg_id order.
CREATE INDEX IF NOT EXISTS messages_by_listing
    ON messages (chat_id, listing_album, listing_ref, msg_id);

CREATE TABLE IF NOT EXISTS listings (
    chat_id      INTEGER NOT NULL,
    album        INTEGER NOT NULL,
    ref          INTEGER NOT NULL,
    rev          INTEGER NOT NULL,
    text         TEXT,
    media        TEXT,
    content_hash TEXT,
    version      INTEGER NOT NULL DEFAULT 0,
    removed      INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (chat_id, album, ref)
);
"""


class ListingKey(NamedTuple):
    chat_id: int
    album: bool
    ref: int

    @classmethod
    def from_row(cls, chat_id: int, album: int, ref: int) -> ListingKey:
        return cls(int(chat_id), bool(album), int(ref))

    def workflow_id(self, rev: int) -> str:
        """DBOS workflow IDs are strings; this is the only place a key becomes one."""
        kind = "a" if self.album else "m"
        return f"listing:{self.chat_id}:{kind}{self.ref}:{rev}"


def connect(path: Path) -> sqlite3.Connection:
    """Open the database in autocommit mode; use `transaction` for writes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def migrate(path: Path) -> None:
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Write transaction that takes the write lock up front.

    BEGIN IMMEDIATE avoids the deadlock where two readers both try to
    upgrade to writers; contention turns into busy_timeout waiting.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
