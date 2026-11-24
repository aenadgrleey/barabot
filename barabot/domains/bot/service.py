"""Bot service: own the bot's Telegram connection for the lifetime of the process.

The client is made in `__main__` and shared with `Notifier`, which sends
through it. For now the bot answers /start, which also lets Telegram
deliver to that user; subscription commands go through
`barabot.api.BarabotApi` and come next.
"""

from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient, events

from barabot.core.config import Config

log = logging.getLogger(__name__)

WELCOME = "Hi! Listings that match your subscriptions will arrive here."


class BotService:
    def __init__(self, cfg: Config, client: TelegramClient) -> None:
        self.cfg = cfg
        self.client = client

    async def start(self) -> None:
        token = self.cfg.bot_token_path.read_text(encoding="utf-8").strip()
        await self.client.start(bot_token=token)  # pyright: ignore[reportGeneralTypeIssues]  # a coroutine inside a running loop
        self.client.add_event_handler(self._on_start, events.NewMessage(incoming=True, pattern=r"^/start\b"))
        me = await self.client.get_me()
        log.info("Bot connected as @%s", getattr(me, "username", "?"))

    async def run_forever(self, stop: asyncio.Event) -> None:
        disconnected = self.client.disconnected
        async with asyncio.TaskGroup() as tasks:
            stopping = tasks.create_task(stop.wait())
            await asyncio.wait((stopping, disconnected), return_when=asyncio.FIRST_COMPLETED)
            stopping.cancel()
        if not stop.is_set():
            await disconnected
            raise ConnectionError("Bot disconnected from Telegram")

    async def stop(self) -> None:
        # The client stays: Notifier holds the same object.
        if self.client.is_connected():
            await self.client.disconnect()  # pyright: ignore[reportGeneralTypeIssues]  # a coroutine inside a running loop

    async def _on_start(self, event: events.NewMessage.Event) -> None:
        if event.is_private and event.chat_id is not None:
            await self.client.send_message(event.chat_id, WELCOME)
