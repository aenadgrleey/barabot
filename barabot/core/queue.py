"""Per-table queue helpers for inbox_queue, outbox_queue, command_queue.

Why per-table functions (not a generic ``enqueue(table=..., **fields)``):
- Each queue has its own typed columns. ``enqueue_inbox`` takes chat_id,
  msg_id, text; ``enqueue_outbox`` takes chat_id, text, reply_to_msg_id.
  Static types catch typos and missing fields at the call site.
- The CLI / IDE can autocomplete per-table args without inspecting
  runtime table metadata.

Pattern: claim-then-ack-delete (NOT status='done'):

  PENDING  ──claim()──▶  PROCESSING  ──ack()──▶  row deleted
                            │
                            └──(lease expired)──▶  back to PENDING via re-claim

`claim` is the only state transition that requires care: it must be
atomic against concurrent claimers. We do SELECT-then-UPDATE inside one
transaction, and re-SELECT by `claimed_by` to filter to rows we
actually claimed (another worker may have raced ahead).

`now` is an optional ISO 8601 string used for the lease cutoff and the
`claimed_at` stamp. Tests pass a fixed `now` for determinism; real
callers omit it and we use `datetime.now(timezone.utc).isoformat()`.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any


def _now_iso(now: str | None) -> str:
    return now if now is not None else datetime.now(timezone.utc).isoformat()


def _lease_cutoff_iso(now: str | None, lease_seconds: int) -> str:
    base = (
        datetime.fromisoformat(now) if now is not None else datetime.now(timezone.utc)
    )
    return (base - timedelta(seconds=lease_seconds)).isoformat()


def _row_to_dict(row: sqlite3.Row | tuple) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {k: row[k] for k in row.keys()}
    # tuple — without Row factory, fall back to column names by ordinal.
    return {}


def _claim(
    conn: sqlite3.Connection,
    table: str,
    *,
    limit: int,
    lease_seconds: int,
    now: str | None,
) -> list[dict[str, Any]]:
    """Atomic claim. Returns rows this caller actually took.

    Concurrency: SQLite serialises writes. Two concurrent claims
    issue competing UPDATEs; each row is won by exactly one UPDATE
    because the WHERE clause is evaluated atomically with the row
    lock. RETURNING tells us which ids THIS UPDATE actually claimed.

    Lease semantics: a row in `processing` whose `claimed_at` is
    older than `now - lease_seconds` is reclaimable. A row whose
    lease has not expired is NOT a candidate.

    Implementation (single atomic statement):
      candidate_ids = SELECT id FROM <table>
                      WHERE (status='pending'
                             OR (status='processing' AND claimed_at <= ?))
                      ORDER BY id ASC LIMIT ?
      (sub-select bound to this transaction's read snapshot)
      UPDATE <table>
         SET status='processing', claimed_at=?, claimed_by=?,
             attempts=attempts+1
       WHERE id IN (candidate_ids)
       RETURNING *
    """
    import uuid

    claimed_at = _now_iso(now)
    cutoff = _lease_cutoff_iso(now, lease_seconds)
    worker_id = uuid.uuid4().hex
    with conn:
        cur = conn.execute(
            f"UPDATE {table} "
            f"SET status = 'processing', "
            f"claimed_at = ?, "
            f"claimed_by = ?, "
            f"attempts = attempts + 1 "
            f"WHERE id IN ("
            f"  SELECT id FROM {table} "
            f"  WHERE status = 'pending' "
            f"     OR (status = 'processing' AND claimed_at <= ?) "
            f"  ORDER BY id ASC LIMIT ?"
            f") "
            f"RETURNING *",
            (claimed_at, worker_id, cutoff, limit),
        )
        rows = cur.fetchall()
        cols = [c[0] for c in cur.description] if cur.description else []
        return [dict(zip(cols, r)) for r in rows]


def _ack(conn: sqlite3.Connection, table: str, ids: list[int]) -> int:
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"DELETE FROM {table} WHERE id IN ({placeholders})", ids
    )
    return cur.rowcount


def _nack(
    conn: sqlite3.Connection,
    table: str,
    ids: list[int],
    *,
    error: str,
    now: str | None = None,
) -> int:
    """Mark the given rows as pending again, clearing the worker stamp
    so the next claim picks them up regardless of who retries."""
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE {table} "
        f"SET status = 'pending', "
        f"claimed_at = NULL, "
        f"claimed_by = NULL, "
        f"error = COALESCE(error || ' | ', '') || ? "
        f"WHERE id IN ({placeholders})",
        (error, *ids),
    )
    return cur.rowcount


# ---------------------------------------------------------------------------
# inbox_queue: scraper → processor. Raw chat messages.
# ---------------------------------------------------------------------------


def enqueue_inbox(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    msg_id: int,
    enqueued_at: str,
    text: str,
    chat_title: str | None = None,
    sender: str | None = None,
) -> int:
    """Insert one row. Idempotent on UNIQUE(chat_id, msg_id).

    On conflict returns the EXISTING row's id and does NOT update fields —
    the original enqueue wins so we keep the first-seen enqueued_at.
    """
    cur = conn.execute(
        """
        INSERT INTO inbox_queue
            (chat_id, msg_id, chat_title, sender, text, enqueued_at, status)
        VALUES (?, ?, ?, ?, ?, ?, 'pending')
        ON CONFLICT(chat_id, msg_id) DO NOTHING
        RETURNING id
        """,
        (chat_id, msg_id, chat_title, sender, text, enqueued_at),
    )
    row = cur.fetchone()
    if row is not None:
        return row[0]
    # Conflict: look up the existing id.
    existing = conn.execute(
        "SELECT id FROM inbox_queue WHERE chat_id = ? AND msg_id = ?",
        (chat_id, msg_id),
    ).fetchone()
    return existing[0] if existing else -1


def claim_inbox(
    conn: sqlite3.Connection,
    *,
    limit: int,
    lease_seconds: int,
    now: str | None = None,
) -> list[dict[str, Any]]:
    return _claim(conn, "inbox_queue", limit=limit, lease_seconds=lease_seconds, now=now)


def ack_inbox(conn: sqlite3.Connection, ids: list[int]) -> int:
    return _ack(conn, "inbox_queue", ids)


def nack_inbox(
    conn: sqlite3.Connection,
    ids: list[int],
    *,
    error: str,
    now: str | None = None,
) -> int:
    return _nack(conn, "inbox_queue", ids, error=error, now=now)


# ---------------------------------------------------------------------------
# outbox_queue: any service → bot. Pre-rendered messages ready to send.
# ---------------------------------------------------------------------------


def enqueue_outbox(
    conn: sqlite3.Connection,
    *,
    chat_id: int,
    text: str,
    enqueued_at: str,
    reply_to_msg_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO outbox_queue (chat_id, text, reply_to_msg_id, enqueued_at, status)
        VALUES (?, ?, ?, ?, 'pending')
        RETURNING id
        """,
        (chat_id, text, reply_to_msg_id, enqueued_at),
    )
    row = cur.fetchone()
    return row[0] if row else -1


def claim_outbox(
    conn: sqlite3.Connection,
    *,
    limit: int,
    lease_seconds: int,
    now: str | None = None,
) -> list[dict[str, Any]]:
    return _claim(conn, "outbox_queue", limit=limit, lease_seconds=lease_seconds, now=now)


def ack_outbox(conn: sqlite3.Connection, ids: list[int]) -> int:
    return _ack(conn, "outbox_queue", ids)


def nack_outbox(
    conn: sqlite3.Connection,
    ids: list[int],
    *,
    error: str,
    now: str | None = None,
) -> int:
    return _nack(conn, "outbox_queue", ids, error=error, now=now)


# ---------------------------------------------------------------------------
# command_queue: incoming user-facing commands (audit log).
# ---------------------------------------------------------------------------


def enqueue_command(
    conn: sqlite3.Connection,
    *,
    source: str,
    command: str,
    enqueued_at: str,
    args: str | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO command_queue (source, command, args, enqueued_at, status)
        VALUES (?, ?, ?, ?, 'pending')
        RETURNING id
        """,
        (source, command, args, enqueued_at),
    )
    row = cur.fetchone()
    return row[0] if row else -1


def claim_command(
    conn: sqlite3.Connection,
    *,
    limit: int,
    lease_seconds: int,
    now: str | None = None,
) -> list[dict[str, Any]]:
    return _claim(conn, "command_queue", limit=limit, lease_seconds=lease_seconds, now=now)


def ack_command(conn: sqlite3.Connection, ids: list[int]) -> int:
    return _ack(conn, "command_queue", ids)


def nack_command(
    conn: sqlite3.Connection,
    ids: list[int],
    *,
    error: str,
    now: str | None = None,
) -> int:
    return _nack(conn, "command_queue", ids, error=error, now=now)