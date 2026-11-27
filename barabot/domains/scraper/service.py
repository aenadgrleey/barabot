"""Own the scraper's Telegram connection for the lifetime of the service.

Live NewMessage, MessageEdited and MessageDeleted events and the startup
backfill all go through `MessageIngest`. Photos are downloaded before
ingest, outside its lock, so processing can show them to Jev.

The scraper reads WATCH_CHATS and every chat the `chats` table marks
joined. It asks the database for each event, so a chat that `JoinerService`
joins is read from its next message on, with no state shared between them.
Both get the same Telegram client from `__main__`; the scraper connects and
disconnects it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl import types
from telethon.tl.patched import Message

from barabot.core.config import Config
from barabot.core.db import connect
from barabot.core.setup_hints import SCRAPER_COMMAND, has_local_scraper_authorization
from barabot.domains.scraper.ingest import MessageIngest
from barabot.domains.scraper.model.event import DeletedMessages, SeenMessage
from barabot.domains.scraper.photos import PhotoDownloader

log = logging.getLogger(__name__)


class ScraperService:
    def __init__(
        self, cfg: Config, client: TelegramClient, ingest: MessageIngest, photos: PhotoDownloader | None = None
    ) -> None:
        """Without a photo downloader, photos are stored as descriptors only."""
        self.cfg = cfg
        self.client = client
        self._photos = photos
        self._ingest = ingest
        self._config_chats = frozenset(cfg.watch_chats)
        self._backfill: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await connect_client(self.cfg, self.client)
        # Chats are filtered in the handlers instead of events.NewMessage(chats=...):
        # deletes from basic groups carry no chat_id, and explicit filtering avoids
        # entity resolution through the gateway.
        self.client.add_event_handler(self._on_message, events.NewMessage())
        self.client.add_event_handler(self._on_message, events.MessageEdited())
        self.client.add_event_handler(self._on_deleted, events.MessageDeleted())
        self._backfill = asyncio.create_task(self._run_backfill(), name="scraper-backfill")

    async def run_forever(self, stop: asyncio.Event) -> None:
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
        if self.client.is_connected():
            await self.client.disconnect()  # pyright: ignore[reportGeneralTypeIssues]  # a coroutine inside a running loop

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        seen = to_seen(event.message)
        if seen is not None and await self._watching(seen.chat_id):
            await self._ingest.save_message(await self._with_photo(event.message, seen))

    async def _on_deleted(self, event: events.MessageDeleted.Event) -> None:
        if event.chat_id is not None and not await self._watching(event.chat_id):
            return
        await self._ingest.mark_deleted(DeletedMessages(event.chat_id, list(event.deleted_ids)))

    async def _watching(self, chat_id: int) -> bool:
        if chat_id in self._config_chats:
            return True
        return await asyncio.to_thread(is_joined, self.cfg.db_path, chat_id)

    async def _with_photo(self, msg, seen: SeenMessage) -> SeenMessage:
        """Add the downloaded file to a photo descriptor; a failed download is not fatal."""
        if self._photos is None or not seen.media or seen.media[0].get("type") != "photo":
            return seen
        try:
            info = await self._photos.download(self.client, msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("Could not download photo of %s/%s", seen.chat_id, seen.msg_id, exc_info=True)
            return seen
        if info is None:
            return seen
        return replace(seen, media=[{**seen.media[0], **info}])

    async def _run_backfill(self) -> None:
        """Re-touch recent messages so listings missed while down get processed.

        Deletes that happened while the process was down are not seen:
        iter_messages simply does not return those messages.
        """
        chats = self._config_chats | await asyncio.to_thread(joined_chat_ids, self.cfg.db_path)
        if not chats:
            log.info("No chats to watch yet; the joiner joins them as subscriptions name them")
        since = datetime.now(UTC) - timedelta(hours=self.cfg.backfill_hours)
        for chat_id in sorted(chats):
            count = 0
            try:
                async for msg in self.client.iter_messages(chat_id):
                    if msg.date < since:
                        break
                    seen = to_seen(msg)
                    if seen is not None:
                        await self._ingest.save_message(await self._with_photo(msg, seen))
                        count += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Backfill failed for chat %s", chat_id)
                continue
            log.info("Backfilled %d message(s) from chat %s", count, chat_id)


def is_joined(db_path: Path, chat_id: int) -> bool:
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT 1 FROM chats WHERE chat_id = ? AND status = 'joined' LIMIT 1", (chat_id,)).fetchone()
        return row is not None
    finally:
        conn.close()


def joined_chat_ids(db_path: Path) -> frozenset[int]:
    """Chats joined so far, for the backfill."""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT chat_id FROM chats WHERE status = 'joined' AND chat_id IS NOT NULL")
        return frozenset(row["chat_id"] for row in rows)
    finally:
        conn.close()


def make_client(cfg: Config) -> TelegramClient:
    """The scraper's Telegram client: the tgmux gateway or the local Telethon session."""
    transport = cfg.telegram
    if transport.uses_local_session:
        session = cfg.scraper_session_path
        if not has_local_scraper_authorization(session):
            raise RuntimeError(f"Authorize the local Telegram session with {SCRAPER_COMMAND}")
        return transport.client_type(str(session), cfg.api_id, cfg.api_hash)
    return transport.client_type("barabot")


async def connect_client(cfg: Config, client: TelegramClient) -> None:
    log.info("Connecting scraper through %s", cfg.telegram.backend)
    await client.connect()
    if cfg.telegram.uses_local_session and not await client.is_user_authorized():
        raise RuntimeError(f"Telegram authorization expired. Run {SCRAPER_COMMAND}")


def to_seen(msg: object) -> SeenMessage | None:
    """Convert a Telethon message; service messages are not listings."""
    # Telethon swaps types.Message for this subclass at import; only it declares chat_id and sender_id.
    if not isinstance(msg, Message):
        return None
    return SeenMessage(
        chat_id=msg.chat_id,  # pyright: ignore[reportArgumentType]  # every message has a peer
        msg_id=msg.id,
        grouped_id=msg.grouped_id,
        sender_id=msg.sender_id,
        date=msg.date,  # pyright: ignore[reportArgumentType]  # optional in the schema, set on every message
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
    if isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        return [{"type": "document", "id": media.document.id, "mime": media.document.mime_type}]
    return [{"type": type(media).__name__}]
