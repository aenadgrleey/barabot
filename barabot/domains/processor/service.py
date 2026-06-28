"""Processor service. Empty stub."""
from __future__ import annotations

import asyncio

from barabot.core.service import Service


class ProcessorService:
    def __init__(self, cfg, *, conn=None) -> None:
        self.cfg = cfg
        self.conn = conn

    async def start(self) -> None:
        pass

    async def run_forever(self, stop: asyncio.Event) -> None:
        await stop.wait()

    async def stop(self) -> None:
        pass