"""`Notifier`: tell users about listings that match their subscriptions.

A user gets one bot message per listing. It is sent once and edited after
that, so `deliveries` keeps its ID and what it shows.

Every listing run that saved its decisions calls `enqueue` once, from its
workflow body. The notifier works out the rest itself:

    notify(listing, version)    workflow on notify_queue, ID
                                notify:{chat}:{a|m}{ref}:{rev}, a child of
                                the listing run
      recipients                users with a match in this version or a
                                message about this listing
      deliver(user), per user   compare what the user should see with what
                                `deliveries` says they see; send, edit or
                                nothing; record

    the user should see        they see              message
    matched                    nothing               send
    matched                    an older version,     edit, silent
                               unmatched or removed
    unmatched                  matched               edit with a note
    removed                    matched or unmatched  edit with a note

A message is a short line and a link to the original post; Telegram's
link preview shows the post itself. Previews work for public chats, where
the link goes through the username. A private chat's t.me/c link opens
only for members and gets no preview.

An edit does not notify the user in Telegram, so "unmatched" and "removed"
are visible only to someone who opens the message.

Sending and recording share one step. Cancelling the listing run cancels
its children between steps, so a message cannot be sent and then left
unrecorded by a cancel; only a crash inside the step can send it twice.
Because `deliver` compares with `deliveries` every time, running notify
again for the same version does nothing new. The processor relies on
that: it notifies after every run, even when the listing did not change,
which recovers a notify cancelled together with an earlier run.

The notifier is a DBOS configured instance like `ListingProcessor`:
construct it before `DBOS.launch()`, call `open()` after.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dbos import DBOS, DBOSConfiguredInstance, Queue, SetWorkflowID
from dbos._error import DBOSMaxStepRetriesExceeded

from barabot.core.chats import message_link
from barabot.core.db import ListingKey, connect, transaction
from barabot.domains.bot import reach

log = logging.getLogger(__name__)

NOTIFY_QUEUE = "notify"
MATCHED, UNMATCHED, REMOVED = "matched", "unmatched", "removed"

# What the bot says. The link to the post follows on its own line.
MATCHED_TEXT = "Matches {queries}"
UNMATCHED_TEXT = "Edited, no longer matches"
REMOVED_TEXT = "The listing was deleted"


class Undeliverable(Exception):
    """The user blocked the bot, never started it, or deleted the message; retrying will not help."""


class BotSender(Protocol):
    async def send(self, user_id: int, text: str) -> int:
        """Send a message and return its ID."""
        ...

    async def edit(self, user_id: int, message_id: int, text: str) -> None: ...


@dataclass(frozen=True)
class Shown:
    """A row of `deliveries`: what the user's message shows."""

    version: int
    state: str  # MATCHED | UNMATCHED | REMOVED
    message_id: int


@dataclass(frozen=True)
class Outgoing:
    """What `deliver` sends: a new message when message_id is None, an edit otherwise."""

    state: str
    message_id: int | None
    message: str


def target(removed: bool, matched: bool, version: int, shown: Shown | None) -> str | None:
    """The state the user's message must switch to, or None if it needs no change."""
    state = REMOVED if removed else MATCHED if matched else UNMATCHED
    if shown is None:
        return MATCHED if state == MATCHED else None
    if state == MATCHED:
        return None if shown.state == MATCHED and shown.version >= version else MATCHED
    if state == UNMATCHED:
        return UNMATCHED if shown.state == MATCHED else None
    return None if shown.state == REMOVED else REMOVED


def render(state: str, link: str | None, queries: list[str]) -> str:
    if state == MATCHED:
        line = MATCHED_TEXT.format(queries=", ".join(f'"{q}"' for q in queries))
    elif state == UNMATCHED:
        line = UNMATCHED_TEXT
    else:
        line = REMOVED_TEXT
    return f"{line}\n{link}" if link else line


def workflow_id(key: ListingKey, rev: int) -> str:
    return f"notify:{key.chat_id}:{'a' if key.album else 'm'}{key.ref}:{rev}"


@DBOS.dbos_class("notifier")
class Notifier(DBOSConfiguredInstance):
    def __init__(self, db_path: Path, sender: BotSender, *, name: str = "default") -> None:
        super().__init__(config_name=name)
        self._db_path = db_path
        self._sender = sender
        self._queue: Queue | None = None

    async def open(self) -> None:
        """Register the queue. Call after DBOS.launch()."""
        # One notify at a time sends one message at a time, which stays
        # under the Bot API's 30 messages a second.
        self._queue = await DBOS.register_queue_async(NOTIFY_QUEUE, worker_concurrency=1, polling_interval_sec=0.05)

    async def enqueue(self, key: ListingKey, rev: int, version: int) -> None:
        """Call from the listing workflow's body, so the notify becomes its child."""
        if self._queue is None:
            raise RuntimeError("Notifier.open() was not called")
        with SetWorkflowID(workflow_id(key, rev)):
            await self._queue.enqueue_async(self.notify, key, version)

    @DBOS.workflow()
    async def notify(self, key: ListingKey, version: int) -> str:
        users = await self.recipients(key, version)
        if users is None:
            return "stale"
        results = []
        for user in users:
            try:
                results.append(f"{user} {await self.deliver(key, version, user)}")
            except DBOSMaxStepRetriesExceeded:
                # Retries ran out, for example Telegram was down. The next
                # run of this listing tries again; the other users go on.
                log.exception("Could not notify user %s about %s", user, key)
                results.append(f"{user} failed")
        return ", ".join(results) or "nobody"

    @DBOS.step()
    async def recipients(self, key: ListingKey, version: int) -> list[int] | None:
        """None when a newer version exists."""
        return await asyncio.to_thread(self._recipients, key, version)

    # Retries cover a bot that is not connected yet, for example runs
    # recovered at launch, and network errors. Telethon sleeps through
    # short flood waits itself.
    @DBOS.step(retries_allowed=True, max_attempts=8, interval_seconds=2.0, backoff_rate=2.0)
    async def deliver(self, key: ListingKey, version: int, user_id: int) -> str:
        outgoing = await asyncio.to_thread(self._prepare, key, version, user_id)
        if outgoing is None:
            return "unchanged"
        try:
            if outgoing.message_id is None:
                message_id = await self._sender.send(user_id, outgoing.message)
            else:
                message_id = outgoing.message_id
                await self._sender.edit(user_id, message_id, outgoing.message)
        except Undeliverable as exc:
            log.warning("Cannot notify user %s about %s: %s", user_id, key, exc)
            await asyncio.to_thread(reach.mark_unreachable, self._db_path, user_id, str(exc))
            return "undeliverable"
        await asyncio.to_thread(self._record, key, version, user_id, outgoing, message_id)
        return "sent" if outgoing.message_id is None else "edited"

    # Blocking halves of the steps; they run in a worker thread.

    def _recipients(self, key: ListingKey, version: int) -> list[int] | None:
        conn = connect(self._db_path)
        try:
            if not _is_latest(conn, key, version):
                return None
            rows = conn.execute(
                "SELECT s.user_id FROM matches m JOIN subscriptions s ON s.id = m.subscription_id "
                "WHERE m.chat_id = ? AND m.album = ? AND m.ref = ? AND m.version = ? AND m.matched = 1 "
                "AND s.active = 1 "
                "UNION SELECT user_id FROM deliveries WHERE chat_id = ? AND album = ? AND ref = ? "
                "ORDER BY 1",
                (*key, version, *key),
            ).fetchall()
        finally:
            conn.close()
        return [row[0] for row in rows]

    def _prepare(self, key: ListingKey, version: int, user_id: int) -> Outgoing | None:
        """What to send now, or None if the version is stale or the message already shows the right thing."""
        conn = connect(self._db_path)
        try:
            listing = conn.execute(
                "SELECT version, removed FROM listings WHERE chat_id = ? AND album = ? AND ref = ?", key
            ).fetchone()
            if listing is None or listing["version"] != version:
                return None
            queries = [
                r["query"]
                for r in conn.execute(
                    "SELECT s.query FROM matches m JOIN subscriptions s ON s.id = m.subscription_id "
                    "WHERE m.chat_id = ? AND m.album = ? AND m.ref = ? AND m.version = ? AND m.matched = 1 "
                    "AND s.active = 1 AND s.user_id = ? ORDER BY s.id",
                    (*key, version, user_id),
                )
            ]
            row = conn.execute(
                "SELECT version, state, bot_message_id FROM deliveries "
                "WHERE chat_id = ? AND album = ? AND ref = ? AND user_id = ?",
                (*key, user_id),
            ).fetchone()
            shown = Shown(row["version"], row["state"], row["bot_message_id"]) if row else None
            state = target(bool(listing["removed"]), bool(queries), version, shown)
            if state is None:
                return None
            # A deleted post has nothing left to preview.
            link = None if state == REMOVED else _link(conn, key)
        finally:
            conn.close()
        return Outgoing(state, shown.message_id if shown else None, render(state, link, queries))

    def _record(self, key: ListingKey, version: int, user_id: int, outgoing: Outgoing, message_id: int) -> None:
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                conn.execute(
                    "INSERT INTO deliveries (chat_id, album, ref, user_id, version, state, bot_message_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (chat_id, album, ref, user_id) DO UPDATE SET "
                    "version = excluded.version, state = excluded.state, bot_message_id = excluded.bot_message_id, "
                    "updated_at = datetime('now')",
                    (*key, user_id, version, outgoing.state, message_id),
                )
                conn.execute("DELETE FROM bot_unreachable WHERE user_id = ?", (user_id,))
        finally:
            conn.close()


def _is_latest(conn: sqlite3.Connection, key: ListingKey, version: int) -> bool:
    row = conn.execute("SELECT version FROM listings WHERE chat_id = ? AND album = ? AND ref = ?", key).fetchone()
    return row is not None and row["version"] == version


def _link(conn: sqlite3.Connection, key: ListingKey) -> str | None:
    """Link to the listing's first live message, by username when the chat has a public one."""
    msg = conn.execute(
        "SELECT min(msg_id) AS msg_id FROM messages "
        "WHERE chat_id = ? AND listing_album = ? AND listing_ref = ? AND deleted = 0",
        key,
    ).fetchone()
    if msg is None or msg["msg_id"] is None:
        return None
    chat = conn.execute(
        "SELECT value FROM chats WHERE chat_id = ? AND kind = 'username' ORDER BY id LIMIT 1", (key.chat_id,)
    ).fetchone()
    return message_link(key.chat_id, msg["msg_id"], chat["value"] if chat else None)
