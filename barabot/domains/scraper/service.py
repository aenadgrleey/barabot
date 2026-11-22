"""Scraper service. Empty stub."""
from __future__ import annotations

import asyncio

from telethon import TelegramClient

from barabot.core.config import Config
from barabot.core.service import Service
from barabot.domains.scraper.source.source import Source


class ScraperService(Service):
    def __init__(self, cfg: Config, sources: tuple[Source, ...], dispatcher: ) -> None:
        self.cfg = cfg
        self.sources = sources


    async def start(self) -> None:
        pass

    async def run_forever(self, stop: asyncio.Event) -> None:
        await stop.wait()

    async def stop(self) -> None:
        pass
