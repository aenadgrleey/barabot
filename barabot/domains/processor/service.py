"""Processor service — placeholder until real ingest pipeline lands.

Will eventually consume `UnifiedItem`s from the scraper, embed them,
dedup against the SQLite store, and (for matching items) emit outbox
events the bot domain polls. Today it's a no-op stub that follows the
same start / run_forever / stop lifecycle as every other service.
"""
from __future__ import annotations

import asyncio
import logging

from barabot.core.service import Service

log = logging.getLogger("barabot.processor")


class ProcessorService:
    """Stub. Replace with real embed+dedup pipeline when ready."""

    def __init__(self, cfg, *, conn=None) -> None:
        self.cfg = cfg
        self.conn = conn

    async def start(self) -> None:
        log.info("processor (stub) start")

    async def run_forever(self, stop: asyncio.Event) -> None:
        try:
            await stop.wait()
        finally:
            pass

    async def stop(self) -> None:
        log.info("processor (stub) stop")