"""Bot service — placeholder until real outbox-poller lands.

Will eventually own a Telegram Bot API client (aiogram/PTB), poll
`outbox_events` from the processor via the `Client` Protocol, render
hits for users, and ack events. Today it's a no-op stub that follows
the same start / run_forever / stop lifecycle as every other service.

Note: this is a *runtime* service — it lives in the same process as
scraper and processor and starts/stops alongside them. It is NOT the
old standalone `reporter/` Bot API process (removed in 813d859). If
we ever need to split bot into its own process, that's a separate
refactor — the Protocol-driven composer here would not change.
"""
from __future__ import annotations

import asyncio
import logging

from barabot.core.service import Service

log = logging.getLogger("barabot.bot")


class BotService:
    """Stub. Replace with real outbox-poller + Bot API client when ready."""

    def __init__(self, cfg, *, client=None) -> None:
        self.cfg = cfg
        # `client` will be the `Client` Protocol impl from domains/bot/client.py
        # (the outbox-poller facade). None today because we're a stub.
        self.client = client

    async def start(self) -> None:
        log.info("bot (stub) start")

    async def run_forever(self, stop: asyncio.Event) -> None:
        try:
            await stop.wait()
        finally:
            pass

    async def stop(self) -> None:
        log.info("bot (stub) stop")