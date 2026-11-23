"""Own the scraper's Telegram connection for the lifetime of the service.

Live NewMessage, MessageEdited and MessageDeleted events and the startup
backfill all go through `MessageIngest`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from telethon import TelegramClient, events
from telethon.tl import types

from barabot.core.config import Config
from barabot.core.setup_hints import SCRAPER_COMMAND, has_local_scraper_authorization
from barabot.domains.scraper.ingest import MessageIngest
from barabot.domains.scraper.model.event import DeletedMessages, SeenMessage

log = logging.getLogger(__name__)


class ScraperService:
    def __init__(self, cfg: Config, ingest: MessageIngest) -> None:
        self.cfg = cfg
        self.client: TelegramClient | None = None
        self._ingest = ingest
        self._chats: frozenset[int] = frozenset(cfg.watch_chats)
        self._backfill: asyncio.Task[None] | None = None

    async def start(self) -> None:
        transport = self.cfg.telegram
        log.info("Connecting scraper through %s", transport.backend)
        if transport.uses_local_session:
            session = self.cfg.scraper_session_path
            if not has_local_scraper_authorization(session):
                raise RuntimeError(f"Authorize the local Telegram session with {SCRAPER_COMMAND}")
            self.client = transport.client_type(str(session), self.cfg.api_id, self.cfg.api_hash)
        else:
            self.client = transport.client_type("barabot")
        await self.client.connect()
        if transport.uses_local_session and not await self.client.is_user_authorized():
            raise RuntimeError(f"Telegram authorization expired. Run {SCRAPER_COMMAND}")

        if not self._chats:
            log.warning("WATCH_CHATS is empty; the scraper will not ingest anything")
        # Chats are filtered in the handlers instead of events.NewMessage(chats=...):
        # deletes from basic groups carry no chat_id, and explicit filtering avoids
        # entity resolution through the gateway.
        self.client.add_event_handler(self._on_message, events.NewMessage())
        self.client.add_event_handler(self._on_message, events.MessageEdited())
        self.client.add_event_handler(self._on_deleted, events.MessageDeleted())
        self._backfill = asyncio.create_task(self._run_backfill(), name="scraper-backfill")

    async def run_forever(self, stop: asyncio.Event) -> None:
        assert self.client is not None
        disconnected = self.client.disconnected
        async with asyncio.TaskGroup() as tasks:
            stopping = tasks.create_task(stop.wait())
            await asyncio.wait((stopping, disconnected), return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
        if not stop.is_set():
            await disconnected
            raise ConnectionError("Scraper disconnected from Telegram")

    async def stop(self) -> None:
        if self._backfill is not None:
            self._backfill.cancel()
            await asyncio.gather(self._backfill, return_exceptions=True)
            self._backfill = None
        if self.client is not None:
            await self.client.disconnect()
            self.client = None

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        seen = to_seen(event.message)
        if seen is not None and seen.chat_id in self._chats:
            await self._ingest.save_message(seen)

    async def _on_deleted(self, event: events.MessageDeleted.Event) -> None:
        if event.chat_id is not None and event.chat_id not in self._chats:
            return
        await self._ingest.mark_deleted(DeletedMessages(event.chat_id, list(event.deleted_ids)))

    async def _run_backfill(self) -> None:
        """Re-touch recent messages so listings missed while down get processed.

        Deletes that happened while the process was down are not seen:
        iter_messages simply does not return those messages.
        """
        since = datetime.now(UTC) - timedelta(hours=self.cfg.backfill_hours)
        for chat_id in sorted(self._chats):
            count = 0
            try:
                async for msg in self.client.iter_messages(chat_id):
                    if msg.date < since:
                        break
                    seen = to_seen(msg)
                    if seen is not None:
                        await self._ingest.save_message(seen)
                        count += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Backfill failed for chat %s", chat_id)
                continue
            log.info("Backfilled %d message(s) from chat %s", count, chat_id)


def to_seen(msg: object) -> SeenMessage | None:
    """Convert a Telethon message; service messages are not listings."""
    if not isinstance(msg, types.Message):
        return None
    return SeenMessage(
        chat_id=msg.chat_id,
        msg_id=msg.id,
        grouped_id=msg.grouped_id,
        sender_id=msg.sender_id,
        date=msg.date,
        text=msg.message or "",
        media=describe_media(msg.media),
    )


def describe_media(media: object) -> list[dict]:
    """Identity of attached media, enough to notice when it changes.

    How the bot forwards media is decided in the bot slice. Link
    previews are not part of a listing.
    """
    if media is None or isinstance(media, types.MessageMediaWebPage):
        return []
    if isinstance(media, types.MessageMediaPhoto) and media.photo is not None:
        return [{"type": "photo", "id": media.photo.id}]
    if isinstance(media, types.MessageMediaDocument) and media.document is not None:
        return [{"type": "document", "id": media.document.id, "mime": media.document.mime_type}]
    return [{"type": type(media).__name__}]
