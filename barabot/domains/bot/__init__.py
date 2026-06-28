"""The bot domain.

Two responsibilities, both pure-data and I/O-free today:

  1. The Protocol `Client` (outbox-poller facade). The processor will
     implement it; the bot types against it. Swappable for tests.
  2. The runtime `BotService` stub — same start / run_forever / stop
     lifecycle as ScraperService and ProcessorService so the composer
     in `__main__.py` treats all three uniformly.

Today both are stubs; real impls land in follow-ups. The split lets
us type the bot domain's contracts (Hit, Message, Subscription,
HitEvent, Client) without bringing in any Telegram-client imports.
"""
from barabot.domains.bot.client import Client
from barabot.domains.bot.model import (
    Hit,
    HitCreated,
    HitEvent,
    HitRemoved,
    HitUpdated,
    Message,
    Subscription,
)
from barabot.domains.bot.service import BotService

__all__ = [
    "BotService",
    "Client",
    "Hit",
    "Message",
    "Subscription",
    "HitCreated",
    "HitUpdated",
    "HitRemoved",
    "HitEvent",
]