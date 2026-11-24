"""Common lifecycle contract every long-running service implements.

All three domains — scraper, processor, bot — run side-by-side in one
process under one TaskGroup. They share the same shape so `__main__.py`
can treat them uniformly:

  1. async start()        — open resources, attach handlers, start tasks
  2. async run_forever()  — block until the shared stop event fires
  3. async stop()         — symmetric teardown (called from a finally)

`start` and `stop` are pre/post hooks. `run_forever` should NOT do
real work — it's a single point that awaits `stop`, and any actual
work happens in tasks spawned inside `start`.

Why a Protocol and not just "implement these methods":
- Type-checks at composer time so a missing `run_forever(stop)` blows
  up immediately, not at the first `asyncio.gather`.
- One place to document the contract; `__main__.py` doesn't need to
  know which domain is which.
- Easy to swap a stub for a real impl without touching the composer.
"""

from __future__ import annotations

import asyncio
from typing import Protocol


class Service(Protocol):
    async def start(self) -> None: ...

    async def run_forever(self, stop: asyncio.Event) -> None: ...

    async def stop(self) -> None: ...
