"""Scraper service — placeholder until real Telegram-listener lands.

Will eventually own the Telethon client, attach NewMessage/MessageEdited
handlers, and push `UnifiedItem`s into the processor pipeline. Today it's
a no-op stub that runs inside an `asyncio.TaskGroup` and awaits a single
`asyncio.Event` for shutdown.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("barabot.scraper")


class ScraperService:
    """Stub. Replace with real Telegram listener when ready."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg

    async def run(self, stop: asyncio.Event) -> None:
        log.info("scraper (stub) start")
        try:
            await stop.wait()
        finally:
            log.info("scraper (stub) stop")