"""Read-only access to barabot's items table from the reporter process.

We open the DB in read-only mode (mode=ro via URI) so we can never
accidentally write to barabot's data. WAL is still respected because
the WAL file is shared.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_ro(path: Path) -> sqlite3.Connection:
    """Open the DB read-only. Raises if the file is missing or locked badly."""
    if not path.exists():
        raise RuntimeError(f"db not found at {path} — start barabot first")
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    # Don't run PRAGMA journal_mode here — we're a read-only connection and
    # sqlite refuses most pragmas in ro mode. The default (deferred) is fine.
    return conn


def fetch_recent(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Return the most recent items, newest first."""
    rows = conn.execute(
        """
        SELECT msg_id, chat_id, sender, text, created_at
        FROM items
        ORDER BY rowid DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "msg_id": r[0], "chat_id": r[1], "sender": r[2],
            "text": r[3], "created_at": r[4],
        }
        for r in rows
    ]


def max_rowid(conn: sqlite3.Connection) -> int:
    """Highest rowid in items. Used to detect new inserts."""
    row = conn.execute("SELECT IFNULL(MAX(rowid), 0) FROM items").fetchone()
    return int(row[0])


def fetch_since(conn: sqlite3.Connection, since_rowid: int) -> list[dict]:
    """Return items with rowid > since_rowid, oldest first (so we can stream)."""
    rows = conn.execute(
        """
        SELECT rowid, msg_id, chat_id, sender, text, created_at
        FROM items
        WHERE rowid > ?
        ORDER BY rowid ASC
        """,
        (since_rowid,),
    ).fetchall()
    return [
        {
            "rowid": r[0], "msg_id": r[1], "chat_id": r[2],
            "sender": r[3], "text": r[4], "created_at": r[5],
        }
        for r in rows
    ]


def chat_counts(conn: sqlite3.Connection) -> list[tuple[int, int]]:
    """[(chat_id, count), ...] for every chat that has items."""
    rows = conn.execute(
        "SELECT chat_id, COUNT(*) FROM items GROUP BY chat_id ORDER BY 2 DESC"
    ).fetchall()
    return [(int(r[0]), int(r[1])) for r in rows]


def search(conn: sqlite3.Connection, needle: str, limit: int = 25) -> list[dict]:
    """LIKE-based substring search. Slop — no FTS, no ranking."""
    rows = conn.execute(
        """
        SELECT msg_id, chat_id, sender, text, created_at
        FROM items
        WHERE text LIKE ?
        ORDER BY rowid DESC
        LIMIT ?
        """,
        (f"%{needle}%", limit),
    ).fetchall()
    return [
        {
            "msg_id": r[0], "chat_id": r[1], "sender": r[2],
            "text": r[3], "created_at": r[4],
        }
        for r in rows
    ]


def get_item(conn: sqlite3.Connection, chat_id: int, msg_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT msg_id, chat_id, sender, text, created_at
        FROM items
        WHERE chat_id = ? AND msg_id = ?
        """,
        (chat_id, msg_id),
    ).fetchone()
    if not row:
        return None
    return {
        "msg_id": row[0], "chat_id": row[1], "sender": row[2],
        "text": row[3], "created_at": row[4],
    }
