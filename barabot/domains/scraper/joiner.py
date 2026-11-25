"""Join the chats that subscriptions name, so the scraper can read them.

Saving a subscription only stores its chats as `pending` (see
barabot/domains/bot/subscriptions.py). `ChatJoiner` runs inside the
scraper, on its Telegram client: through tgmux, a join made by the
barabot integration is also what assigns the chat to barabot. Each round
it takes the chats that an active subscription points to and that are

    pending     not tried yet
    requested   a join request waits for an admin; tried again every REQUESTED_RETRY
    failed      tried again every FAILED_RETRY, in case the chat changed

joins them one at a time and writes back the status, the chat ID and the
title. A joined chat is handed to `on_joined`, which adds it to the
scraper's watch list right away.

Telegram limits joins, so there is a pause between them, and a FloodWait
ends the round and waits as long as Telegram asks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import errors, functions, types, utils

from barabot.core.chats import ChatId, ChatRef, ChatStatus, Invite, Username, ref_from_row
from barabot.core.db import connect, transaction

log = logging.getLogger(__name__)

ROUND_INTERVAL = 30.0  # seconds between rounds when there is nothing to do
JOIN_PAUSE = 10.0  # seconds between two joins
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


def _joined(entity: Any) -> Outcome:
    return Outcome(ChatStatus.JOINED, utils.get_peer_id(entity), getattr(entity, "title", None))


async def join(client: Any, ref: ChatRef, watched: Collection[int]) -> Outcome:
    """Join one chat on the scraper's client. FloodWaitError and unknown errors propagate."""
    try:
        match ref:
            case ChatId(chat_id) if chat_id in watched:
                # Already read through WATCH_CHATS; no need to ask Telegram.
                return Outcome(ChatStatus.JOINED, chat_id)
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
        watched: Collection[int],
        on_joined: Callable[[int], None],
        *,
        pause: float = JOIN_PAUSE,
        interval: float = ROUND_INTERVAL,
    ) -> None:
        self._db_path = db_path
        self._client = client
        self._watched = watched  # the scraper's live watch list; read only here
        self._on_joined = on_joined
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
            outcome = await join(self._client, chat.ref, self._watched)
            await asyncio.to_thread(self._save, chat.id, outcome)
            if outcome.status is ChatStatus.JOINED and outcome.chat_id is not None:
                log.info("Joined %s (%s) as chat %s", chat.ref, outcome.title or "?", outcome.chat_id)
                self._on_joined(outcome.chat_id)
            elif outcome.status is ChatStatus.REQUESTED:
                log.info("Asked to join %s; waiting for an admin", chat.ref)
            else:
                log.warning("Cannot join %s: %s", chat.ref, outcome.reason)
        return len(due)

    def joined_chat_ids(self) -> set[int]:
        """Chats joined before, to watch from the start."""
        conn = connect(self._db_path)
        try:
            rows = conn.execute("SELECT chat_id FROM chats WHERE status = 'joined' AND chat_id IS NOT NULL")
            return {row["chat_id"] for row in rows}
        finally:
            conn.close()

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
