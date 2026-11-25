"""Bot service: own the bot's Telegram connection for the lifetime of the process.

It gets the bot as a `TelethonSender` from `__main__` and connects its
client; `Notifier` and the MCP connector send through the same object. For now the bot answers /start, which also lets Telegram
deliver to that user; subscription commands go through
`barabot.api.BarabotApi` and come next.
"""

from __future__ import annotations

import asyncio
import html
import logging

from telethon import events

from barabot.core.config import Config
from barabot.domains.bot import reach
from barabot.domains.bot.client import TelethonSender

log = logging.getLogger(__name__)

WELCOME = "Hi! Listings that match your subscriptions will arrive here."
CONNECT_HINT = (
    "\n\nSet up subscriptions by talking to your assistant: in Claude, open Settings &gt; Connectors &gt; "
    "Add custom connector, paste this link and log in with Telegram:\n<code>{url}</code>"
)


def welcome_text(cfg: Config) -> str:
    """The /start reply in HTML. The connector link is inline code, which Telegram copies on tap."""
    return html.escape(WELCOME) + CONNECT_HINT.format(url=html.escape(f"{cfg.mcp.public_url}/mcp"))


class BotService:
    def __init__(self, cfg: Config, bot: TelethonSender) -> None:
        self.cfg = cfg
        self.client = bot.client

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
        # The client stays: Notifier and the MCP connector hold the same bot.
        if self.client.is_connected():
            await self.client.disconnect()  # pyright: ignore[reportGeneralTypeIssues]  # a coroutine inside a running loop

    async def _on_start(self, event: events.NewMessage.Event) -> None:
        if event.is_private and event.chat_id is not None:
            await self.client.send_message(event.chat_id, welcome_text(self.cfg), parse_mode="html", link_preview=False)
            # Start (or unblocking and pressing Start) means the bot can write again.
            await asyncio.to_thread(reach.mark_reachable, self.cfg.db_path, event.chat_id)
