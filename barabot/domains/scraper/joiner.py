"""Join the chats that subscriptions name, so the scraper can read them.

Saving a subscription only stores its chats as `pending` (see
barabot/domains/bot/subscriptions.py). `JoinerService` runs `ChatJoiner`
as its own service on the scraper's Telegram client, which `__main__`
hands to both: through tgmux, a join made by the barabot integration is
also what assigns the chat to barabot. Each round
it takes the chats that an active subscription points to and that are

    pending     not tried yet
    requested   a join request waits for an admin; tried again every REQUESTED_RETRY
    failed      tried again every FAILED_RETRY, in case the chat changed

joins them one at a time and writes back the status, the chat ID and the
title. The database is the only link to the scraper: it reads every chat
marked joined. The scraper connects the client, so it starts before the
joiner and stops after it.

A folder link (`t.me/addlist/<slug>`) is read once: its channels are
joined like any other chat, each becomes a `chats` row whose `parent_id`
is the folder row, and the folder row becomes `expanded`. Later edits to
the folder are not followed.

Telegram limits joins, so there is a pause between them, and a FloodWait
ends the round and waits as long as Telegram asks.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import errors, functions, types, utils

from barabot.core.chats import ChatId, ChatList, ChatRef, ChatStatus, Invite, Username, ref_from_row
from barabot.core.db import connect, transaction

log = logging.getLogger(__name__)

ROUND_INTERVAL = 30.0  # seconds between rounds when there is nothing to do
JOIN_PAUSE = 10.0  # seconds between two joins
MAX_LIST_CHATS = 50  # channels taken from one folder link
REQUESTED_RETRY = "-15 minutes"  # SQLite datetime modifiers
FAILED_RETRY = "-1 day"

# Telegram's answers that mean this reference will not get us into a chat.
_GONE = (
    errors.UsernameNotOccupiedError,
    errors.UsernameInvalidError,
    errors.InviteHashExpiredError,
    errors.InviteHashInvalidError,
    errors.InviteHashEmptyError,
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.ChatInvalidError,
    errors.UserBannedInChannelError,
)

# Telethon 1.46 has no classes for a dead folder link, so the RPC message is
# all we get. Without this, such a link would raise every round forever.
_SLUG_GONE = ("INVITE_SLUG_EXPIRED", "INVITE_SLUG_EMPTY")


@dataclass(frozen=True)
class Outcome:
    status: ChatStatus
    chat_id: int | None = None
    title: str | None = None
    reason: str | None = None  # why it failed, for the log


@dataclass(frozen=True)
class Due:
    """A `chats` row to try."""

    id: int
    ref: ChatRef
    status: ChatStatus


@dataclass(frozen=True)
class Child:
    """A chat extracted from a folder link, before its row is written."""

    kind: str
    value: str
    outcome: Outcome


def _joined(entity: Any) -> Outcome:
    return Outcome(ChatStatus.JOINED, utils.get_peer_id(entity), getattr(entity, "title", None))


def _child(channel: types.Channel, outcome: Outcome) -> Child:
    chat_id = utils.get_peer_id(channel)
    if channel.username:
        return Child("username", channel.username.lower(), outcome)
    return Child("id", str(chat_id), outcome)


def _unusable_slug(exc: errors.RPCError) -> bool:
    """True when Telegram says this folder link will never work."""
    if isinstance(exc, _GONE):
        return True
    return any((exc.message or "").startswith(code) for code in _SLUG_GONE)


def _reason(exc: errors.RPCError) -> str:
    if isinstance(exc, _GONE):
        return type(exc).__name__
    return exc.message or type(exc).__name__


async def join(client: Any, ref: ChatRef) -> Outcome:
    """Join one chat on the scraper's client. FloodWaitError and unknown errors propagate."""
    try:
        match ref:
            case ChatId(chat_id):
                try:
                    entity = await client.get_entity(chat_id)
                except ValueError:
                    return Outcome(
                        ChatStatus.FAILED,
                        reason="not a member, and a chat cannot be joined by ID: use @username or an invite link",
                    )
                if getattr(entity, "left", False):
                    await client(functions.channels.JoinChannelRequest(entity))
                return _joined(entity)
            case Username(name):
                try:
                    entity = await client.get_entity(name)
                except ValueError:
                    return Outcome(ChatStatus.FAILED, reason=f"no chat @{name}")
                if not isinstance(entity, types.Channel):
                    return Outcome(ChatStatus.FAILED, reason=f"@{name} is not a group or channel")
                try:
                    await client(functions.channels.JoinChannelRequest(entity))  # pyright: ignore[reportArgumentType]  # Telethon converts the entity
                except errors.UserAlreadyParticipantError:
                    pass
                return _joined(entity)
            case Invite(hash_):
                try:
                    updates = await client(functions.messages.ImportChatInviteRequest(hash_))
                    return _joined(updates.chats[0])
                except errors.UserAlreadyParticipantError:
                    info = await client(functions.messages.CheckChatInviteRequest(hash_))
                    chat = getattr(info, "chat", None)
                    if chat is None:
                        return Outcome(ChatStatus.FAILED, reason="invite says we are a member but shows no chat")
                    return _joined(chat)
    except errors.InviteRequestSentError:
        return Outcome(ChatStatus.REQUESTED)
    except _GONE as exc:
        return Outcome(ChatStatus.FAILED, reason=type(exc).__name__)
    raise AssertionError(f"unknown chat reference {ref!r}")


class ChatJoiner:
    def __init__(
        self,
        db_path: Path,
        client: Any,
        *,
        pause: float = JOIN_PAUSE,
        interval: float = ROUND_INTERVAL,
    ) -> None:
        self._db_path = db_path
        self._client = client
        self._pause = pause
        self._interval = interval

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
                delay = self._interval
            except errors.FloodWaitError as exc:
                log.warning("Telegram asks to wait %ss before joining more chats", exc.seconds)
                delay = max(float(exc.seconds), self._interval)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Joining chats failed; trying again next round")
                delay = self._interval
            await asyncio.sleep(delay)

    async def run_once(self) -> int:
        """Try every due chat once; return how many were tried."""
        due = await asyncio.to_thread(self._due)
        for n, chat in enumerate(due):
            if n:
                await asyncio.sleep(self._pause)
            if isinstance(chat.ref, ChatList):
                await self._expand(chat.id, chat.ref)
                continue
            outcome = await join(self._client, chat.ref)
            await asyncio.to_thread(self._save, chat.id, outcome)
            if outcome.status is ChatStatus.JOINED and outcome.chat_id is not None:
                log.info("Joined %s (%s) as chat %s", chat.ref, outcome.title or "?", outcome.chat_id)
            elif outcome.status is ChatStatus.REQUESTED:
                log.info("Asked to join %s; waiting for an admin", chat.ref)
            else:
                log.warning("Cannot join %s: %s", chat.ref, outcome.reason)
        return len(due)

    async def _expand(self, row_id: int, ref: ChatList) -> None:
        """Read a folder once, join its channels, and write them in one transaction."""
        try:
            result = await self._client(functions.chatlists.CheckChatlistInviteRequest(ref.slug))
        except errors.RPCError as exc:
            if not _unusable_slug(exc):
                raise
            outcome = Outcome(ChatStatus.FAILED, reason=_reason(exc))
            await asyncio.to_thread(self._save, row_id, outcome)
            log.warning("Cannot read folder %s: %s", ref, outcome.reason)
            return
        title = result.title.text if isinstance(result, types.chatlists.ChatlistInvite) else None
        found = [chat for chat in result.chats if isinstance(chat, types.Channel)]
        channels = found[:MAX_LIST_CHATS]
        if len(found) > MAX_LIST_CHATS:
            log.warning(
                "Folder %s has more than %s channels; reading only the first %s", ref, MAX_LIST_CHATS, MAX_LIST_CHATS
            )
        if not channels:
            outcome = Outcome(ChatStatus.FAILED, title=title, reason="the folder contains no channels")
            await asyncio.to_thread(self._save, row_id, outcome)
            log.warning("Cannot expand %s: %s", ref, outcome.reason)
            return
        children: list[Child] = []
        for n, channel in enumerate(channels):
            if n:
                await asyncio.sleep(self._pause)
            children.append(_child(channel, await self._join_channel(channel)))
        joined = await asyncio.to_thread(self._save_expansion, row_id, title, children)
        for chat_id in joined:
            log.info("Joined chat %s from folder %s", chat_id, ref)

    async def _join_channel(self, channel: types.Channel) -> Outcome:
        """Join one channel extracted from a folder. FloodWait and unknown errors propagate."""
        try:
            await self._client(functions.channels.JoinChannelRequest(channel))  # pyright: ignore[reportArgumentType]  # Telethon converts the entity
        except errors.UserAlreadyParticipantError:
            return _joined(channel)
        except errors.InviteRequestSentError:
            return Outcome(ChatStatus.REQUESTED, utils.get_peer_id(channel), channel.title)
        except _GONE as exc:
            return Outcome(ChatStatus.FAILED, utils.get_peer_id(channel), channel.title, type(exc).__name__)
        return _joined(channel)

    def _due(self) -> list[Due]:
        conn = connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT c.id, c.kind, c.value, c.status FROM chats c "
                "WHERE EXISTS (SELECT 1 FROM subscription_chats sc JOIN subscriptions s ON s.id = sc.subscription_id "
                "              WHERE sc.chat = c.id AND s.active = 1) "
                "AND (c.status = 'pending' "
                "     OR (c.status = 'requested' AND c.updated_at <= datetime('now', ?)) "
                "     OR (c.status = 'failed' AND c.updated_at <= datetime('now', ?))) "
                "ORDER BY c.status = 'pending' DESC, c.id",
                (REQUESTED_RETRY, FAILED_RETRY),
            ).fetchall()
            return [Due(row["id"], ref_from_row(row["kind"], row["value"]), ChatStatus(row["status"])) for row in rows]
        finally:
            conn.close()

    def _save(self, row_id: int, outcome: Outcome) -> None:
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE chats SET status = ?, chat_id = COALESCE(?, chat_id), title = COALESCE(?, title), "
                    "updated_at = datetime('now') WHERE id = ?",
                    (outcome.status.value, outcome.chat_id, outcome.title, row_id),
                )
        finally:
            conn.close()

    def _save_expansion(self, parent_id: int, title: str | None, children: list[Child]) -> list[int]:
        """Write the folder row and its children in one transaction; return joined chat IDs."""
        conn = connect(self._db_path)
        joined: list[int] = []
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE chats SET status = ?, title = COALESCE(?, title), updated_at = datetime('now') "
                    "WHERE id = ?",
                    (ChatStatus.EXPANDED.value, title, parent_id),
                )
                for child in children:
                    conn.execute(
                        "INSERT OR IGNORE INTO chats (kind, value, chat_id, title, parent_id, status) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            child.kind,
                            child.value,
                            child.outcome.chat_id,
                            child.outcome.title,
                            parent_id,
                            child.outcome.status.value,
                        ),
                    )
                    conn.execute(
                        "UPDATE chats SET title = ?, parent_id = COALESCE(parent_id, ?), chat_id = ?, status = ?, "
                        "updated_at = datetime('now') WHERE kind = ? AND value = ?",
                        (
                            child.outcome.title,
                            parent_id,
                            child.outcome.chat_id,
                            child.outcome.status.value,
                            child.kind,
                            child.value,
                        ),
                    )
                    child_id = conn.execute(
                        "SELECT id FROM chats WHERE kind = ? AND value = ?", (child.kind, child.value)
                    ).fetchone()["id"]
                    # Every subscription that named the folder also names the extracted chat.
                    conn.execute(
                        "INSERT OR IGNORE INTO subscription_chats (subscription_id, chat) "
                        "SELECT sc.subscription_id, ? FROM subscription_chats sc WHERE sc.chat = ?",
                        (child_id, parent_id),
                    )
                    if child.outcome.status is ChatStatus.JOINED and child.outcome.chat_id is not None:
                        joined.append(child.outcome.chat_id)
        finally:
            conn.close()
        return joined


class JoinerService:
    """`ChatJoiner` as a service: rounds run in a task from start() until stop()."""

    def __init__(self, db_path: Path, client: Any) -> None:
        self._joiner = ChatJoiner(db_path, client)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._joiner.run_forever(), name="joiner")

    async def run_forever(self, stop: asyncio.Event) -> None:
        assert self._task is not None, "start() runs first"
        stopping = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait((stopping, self._task), return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
        if not stop.is_set():
            self._task.result()  # rounds catch errors, so only something fatal ends the task
            raise RuntimeError("Joiner stopped")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
