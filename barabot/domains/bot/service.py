"""Bot service. Empty stub."""

from __future__ import annotations

import asyncio


class BotService:
    def __init__(self, cfg, *, client=None) -> None:
        self.cfg = cfg
        self.client = client

    async def start(self) -> None:
        pass

    async def run_forever(self, stop: asyncio.Event) -> None:
        await stop.wait()

    async def stop(self) -> None:
        pass
