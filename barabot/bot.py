"""The bot itself. Wires everything together; no clever scheduling.

Uses Telethon (not Pyrogram) — the user wanted a single MTProto library and
chose Telethon for the long-term project. Pyrogram-shaped concepts map to
Telethon like this:

  pyrogram.Client          →  telethon.TelegramClient
  pyrogram filters/handlers →  client.add_event_handler(EventNewMessage, ...)
  pyrogram Message         →  telethon custom.Message (newapi=True)
  filters.chat(ids)        →  event.chat and event.is_group / event.is_channel
                              + manual `if event.chat_id not in cfg.watch_chats`
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

from .config import Config, load
from .db import connect
from .locks import _acquire_lock, _release_lock, _remove_pid, _write_pid
from .media_group import UnifiedItem, coalesce
from .processor import IngestMessage, ProcessResult, process_message

log = logging.getLogger("barabot")


def make_client(cfg: Config) -> TelegramClient:
    """Construct an (unstarted) Telethon client.

    `StringSession` is the in-memory analog of Pyrogram's `session_string` —
    we keep the auth key in memory and never touch disk.
    """
    from telethon.sessions import StringSession
    return TelegramClient(
        StringSession(cfg.session_string),
        cfg.api_id,
        cfg.api_hash,
        # sequential_updates keeps handlers from racing on the same event
        sequential_updates=True,
        # 4-connection pool is Telethon's default; we don't need more for
        # one user account.
    )


class Bot:
    """Owns the Telethon client, the DB connection, and the embed semaphore.

    Kept as a class so handlers can be bound without closing over globals,
    and so unit tests can stub pieces out if they want to.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.conn = connect(cfg.db_path)
        self.semaphore = asyncio.Semaphore(cfg.embedding_concurrency)
        self.client = make_client(cfg)
        self._stopping = asyncio.Event()

    # ---- public hooks ----------------------------------------------------

    def install_signal_handlers(self, loop: asyncio.AbstractEventLoop) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._on_signal, sig)

    def _on_signal(self, sig: signal.Signals) -> None:
        log.info("received %s, shutting down", sig.name)
        self._stopping.set()

    def attach_handlers(self) -> None:
        """Register NewMessage / MessageEdited handlers on the client.

        Telethon has no chat-filter primitive like Pyrogram's `filters.chat`,
        so the watch-list check happens inside `_handle_message`.
        """
        self.client.add_event_handler(
            self._handle_new_or_edit,
            events.NewMessage(chats=self.cfg.watch_chats),
        )
        self.client.add_event_handler(
            self._handle_new_or_edit,
            events.MessageEdited(chats=self.cfg.watch_chats),
        )

    # ---- handlers --------------------------------------------------------

    async def _handle_new_or_edit(self, event: events.NewMessage.Event) -> None:
        """Live handler — convert Telethon event into IngestMessage and route
        through the shared `process_message` pipeline. The actual dedup/embed/
        upsert logic lives in `barabot.processor` and is shared with backfill.

        For media groups (albums of N photos with one shared caption), we
        fetch the sibling messages from the same `grouped_id`, coalesce them
        into a single UnifiedItem, and process that — so the whole album is
        one row with `photo_count=N` instead of N rows with empty text.
        """
        message: Message = event.message
        if not message:
            return
        chat_id = event.chat_id
        if chat_id not in self.cfg.watch_chats:
            return

        # Media group? Pull siblings so coalesce sees them all together.
        if message.grouped_id and not message.service:
            try:
                # ids=range(start, end) is the cheap way: ask for a window
                # around message.id. Telethon returns only existing messages.
                siblings = await self.client.get_messages(
                    event.chat,
                    ids=range(max(1, message.id - 10), message.id + 1),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("could not fetch album siblings for msg %s: %s", message.id, exc)
                siblings = [message]
            unified = coalesce(siblings, chat_id)
            # We want the UnifiedItem that *corresponds to this group's
            # lowest msg_id*. coalesce yields one entry per group, so pick
            # the first whose msg_id range covers message.id.
            item = self._pick_unified_for(unified, message.id)
        else:
            item = UnifiedItem(
                chat_id=chat_id,
                msg_id=message.id,
                text=(message.text or message.caption or "").strip(),
                sender=await self._sender_label(event),
                is_service=bool(message.service),
                photo_count=1,
            )

        if not item:
            return
        if not item.text and not item.is_service:
            return

        ingest = IngestMessage(
            chat_id=item.chat_id,
            msg_id=item.msg_id,
            text=item.text,
            sender=item.sender,
            is_service=item.is_service,
            photo_count=item.photo_count,
        )
        outcome = await process_message(
            ingest, conn=self.conn, cfg=self.cfg, semaphore=self.semaphore
        )
        # Log the chat title for live handler output. Backfill already knows
        # the chat and doesn't need this — it's just a label.
        if outcome.result == ProcessResult.NEW:
            chat = await event.get_chat()
            log.info(
                "live NEW: chat_title=%s msg=%s photos=%d score=%.3f",
                getattr(chat, "title", None) or getattr(chat, "username", None),
                message.id, item.photo_count, outcome.score,
            )

    @staticmethod
    def _pick_unified_for(unified: list[UnifiedItem], msg_id: int) -> UnifiedItem | None:
        """Return the UnifiedItem whose `msg_id` (the PK carrier, the lowest
        in the group) is the one Telegram assigned to this event.

        Telethon fires one NewMessage per photo in the album; coalesce
        returns one entry per group. We need to find the one that *this*
        event's message.id is part of — that's the entry whose msg_id is
        the minimum id of all events in the group, which we cannot know
        cheaply here, so we fall back to: return the first unified item
        whose `msg_id` ≤ msg_id (since PK carrier is the lowest).
        """
        candidates = [u for u in unified if u.msg_id <= msg_id]
        if not candidates:
            return unified[0] if unified else None
        # Take the one with the largest msg_id that's still ≤ event's id —
        # that's the most "specific" group containing this event.
        return max(candidates, key=lambda u: u.msg_id)

    async def _sender_label(self, event: events.NewMessage.Event) -> str | None:
        """Best-effort human-readable label for who sent the message."""
        sender = await event.get_sender()
        if sender is None:
            return None
        uname = getattr(sender, "username", None)
        if uname:
            return f"@{uname}"
        first = getattr(sender, "first_name", "") or ""
        last = getattr(sender, "last_name", "") or ""
        full = (first + " " + last).strip()
        return full or None

    # ---- lifecycle -------------------------------------------------------


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Telethon is chatty at INFO (e.g. "Loading connection", "Disconnected").
    # Bump to WARNING so it doesn't drown the actual signal out.
    logging.getLogger("telethon").setLevel(logging.WARNING)


async def run() -> None:
    cfg = load()
    _configure_logging(cfg.log_level)
    if not cfg.watch_chats:
        raise RuntimeError("WATCH_CHATS is empty — set at least one chat id in .env")

    lock_fd = _acquire_lock(cfg.lock_path)
    _write_pid(cfg.pid_path)
    try:
        bot = Bot(cfg)
        bot.attach_handlers()
        loop = asyncio.get_running_loop()
        bot.install_signal_handlers(loop)
        try:
            await bot.client.start()
            log.info(
                "barabot up: watching %d chat(s), threshold=%.2f, db=%s, pid=%d",
                len(cfg.watch_chats), cfg.similarity_threshold, cfg.db_path, os.getpid(),
            )
            await bot._stopping.wait()
        finally:
            if bot.client.is_connected():
                await bot.client.disconnect()
    finally:
        _remove_pid(cfg.pid_path)
        _release_lock(lock_fd, cfg.lock_path)
    log.info("barabot stopped cleanly")