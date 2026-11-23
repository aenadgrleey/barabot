"""Own the scraper's Telegram connection for the lifetime of the service."""
from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient

from barabot.core.config import Config
from barabot.core.setup_hints import SCRAPER_COMMAND, has_local_scraper_authorization

log = logging.getLogger(__name__)


class ScraperService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.client: TelegramClient | None = None

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
        if self.client is not None:
            await self.client.disconnect()
            self.client = None
