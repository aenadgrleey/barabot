"""Scraper service — placeholder until real Telegram-listener lands.

Will eventually own the Telethon client, attach NewMessage/MessageEdited
handlers, and push `UnifiedItem`s into the processor pipeline. Today it's
a no-op stub that follows the same start / run_forever / stop lifecycle
as every other service in the process.
"""
from __future__ import annotations

import asyncio
import logging

from barabot.core.service import Service

log = logging.getLogger("barabot.scraper")


class ScraperService:
    """Stub. Replace with real Telegram listener when ready."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    async def start(self) -> None:
        log.info("scraper (stub) start")

    async def run_forever(self, stop: asyncio.Event) -> None:
        try:
            await stop.wait()
        finally:
            pass  # stop() below handles the visible logging

    async def stop(self) -> None:
        log.info("scraper (stub) stop")