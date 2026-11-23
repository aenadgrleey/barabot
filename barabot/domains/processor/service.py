"""Processor lifecycle while listing matching is under development."""
from __future__ import annotations

import asyncio

from barabot.core.config import Config


class ProcessorService:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def start(self) -> None:
        pass

    async def run_forever(self, stop: asyncio.Event) -> None:
        await stop.wait()

    async def stop(self) -> None:
        pass
