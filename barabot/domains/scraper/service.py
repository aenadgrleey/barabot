"""Scraper service. Empty stub."""
from __future__ import annotations

import asyncio

from barabot.core.service import Service


class ScraperService:
    def __init__(self, cfg) -> None:
        self.cfg = cfg

    async def start(self) -> None:
        pass

    async def run_forever(self, stop: asyncio.Event) -> None:
        await stop.wait()

    async def stop(self) -> None:
        pass