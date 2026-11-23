"""`MessageIngest`: the one way Telegram events enter the system.

Live events and startup backfill both call it. For every event it saves
the message and bumps its listing's revision in one transaction, then
hands the new revision to `schedule`, which cancels the run of `rev - 1`
and enqueues `rev`. So ingestion both persists and starts processing; the two are
one operation.

"Bump" below means `rev = rev + 1` on the listing. The new rev names the
new run, `listing:{key}:{rev}`, and lets us name the run to cancel,
`rev - 1`, without storing it anywhere.

The lock makes that operation atomic with respect to other events.
Telethon and tgmux dispatch each update as its own task, so without it
two events on one listing can interleave: B bumps to 3 and cancels 2
before A, which bumped to 2, has enqueued it; then A enqueues 2 and
both 2 and 3 run. The section takes a few milliseconds.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path

from barabot.core.db import ListingKey, connect, transaction
from barabot.domains.scraper.model.event import DeletedMessages, SeenMessage

Schedule = Callable[[ListingKey, int], Awaitable[None]]

# Marked IDs of channels and supergroups are -100xxxxxxxxxx. Their message
# IDs are numbered per channel, unlike the per-account IDs of basic groups
# and private chats, so a delete without chat_id never refers to them.
_CHANNEL_ID_LIMIT = -1_000_000_000_000

# Save path: creates the listing on its first message, so it is an upsert,
# and takes the key from the stored row so Python never builds it.
_BUMP_FROM_MESSAGE = """
INSERT INTO listings (chat_id, album, ref, rev)
SELECT chat_id, listing_album, listing_ref, 1
FROM messages WHERE chat_id = ? AND msg_id = ?
ON CONFLICT (chat_id, album, ref) DO UPDATE SET rev = rev + 1
RETURNING chat_id, album, ref, rev
"""


class MessageIngest:
    def __init__(self, db_path: Path, schedule: Schedule) -> None:
        self._db_path = db_path
        self._schedule = schedule
        self._lock = asyncio.Lock()

    async def save_message(self, msg: SeenMessage) -> None:
        async with self._lock:
            key, rev = await asyncio.to_thread(self._save_message_blocking, msg)
            await self._schedule(key, rev)

    async def mark_deleted(self, event: DeletedMessages) -> None:
        async with self._lock:
            bumped = await asyncio.to_thread(self._mark_deleted_blocking, event)
            for key, rev in bumped:
                await self._schedule(key, rev)

    # Blocking halves; they run in a worker thread, one transaction each.

    def _save_message_blocking(self, msg: SeenMessage) -> tuple[ListingKey, int]:
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                # grouped_id is left alone on conflict: it never changes in
                # Telegram, and changing it would move the message between listings.
                conn.execute(
                    "INSERT INTO messages (chat_id, msg_id, grouped_id, sender_id, date, text, media) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (chat_id, msg_id) DO UPDATE SET text = excluded.text, media = excluded.media",
                    (
                        msg.chat_id,
                        msg.msg_id,
                        msg.grouped_id,
                        msg.sender_id,
                        msg.date.isoformat(),
                        msg.text,
                        json.dumps(msg.media),
                    ),
                )
                row = conn.execute(_BUMP_FROM_MESSAGE, (msg.chat_id, msg.msg_id)).fetchone()
        finally:
            conn.close()
        return ListingKey.from_row(row["chat_id"], row["album"], row["ref"]), int(row["rev"])

    def _mark_deleted_blocking(self, event: DeletedMessages) -> list[tuple[ListingKey, int]]:
        """Mark known messages deleted and bump each affected listing once.

        One bump per listing matters: if an album lost two photos in one
        event and we bumped twice, `rev - 1` would name a run that never
        existed and the real previous run would survive.
        """
        if not event.msg_ids:
            return []
        marks = ",".join("?" * len(event.msg_ids))
        if event.chat_id is None:
            where = f"msg_id IN ({marks}) AND chat_id > ?"
            args: list[int] = [*event.msg_ids, _CHANNEL_ID_LIMIT]
        else:
            where = f"chat_id = ? AND msg_id IN ({marks})"
            args = [event.chat_id, *event.msg_ids]

        conn = connect(self._db_path)
        try:
            with transaction(conn):
                rows = conn.execute(
                    f"UPDATE messages SET deleted = 1 WHERE deleted = 0 AND {where} "
                    "RETURNING chat_id, listing_album, listing_ref",
                    args,
                ).fetchall()
                keys = dict.fromkeys(ListingKey.from_row(*row) for row in rows)
                return [(key, self._bump_listing(conn, key)) for key in keys]
        finally:
            conn.close()


    @staticmethod
    def _bump_listing(conn: sqlite3.Connection, key: ListingKey) -> int:
        """Delete path: the listing already exists, so a plain UPDATE."""
        row = conn.execute(
            "UPDATE listings SET rev = rev + 1 WHERE chat_id = ? AND album = ? AND ref = ? RETURNING rev",
            key,
        ).fetchone()
        return int(row["rev"])
