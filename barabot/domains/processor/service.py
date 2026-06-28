"""Processor service — placeholder until real ingest pipeline lands.

Will eventually consume `UnifiedItem`s from the scraper, embed them,
dedup against the SQLite store, and (for matching items) emit outbox
events the bot domain polls. Today it's a no-op stub that runs inside
an `asyncio.TaskGroup` and awaits a single `asyncio.Event` for shutdown.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("barabot.processor")


class ProcessorService:
    """Stub. Replace with real embed+dedup pipeline when ready."""

    def __init__(self, cfg, *, conn=None) -> None:
        self.cfg = cfg
        self.conn = conn

    async def run(self, stop: asyncio.Event) -> None:
        log.info("processor (stub) start")
        try:
            await stop.wait()
        finally:
            log.info("processor (stub) stop")