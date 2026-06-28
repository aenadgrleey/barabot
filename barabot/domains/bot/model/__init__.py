"""Data models for the user-facing bot domain.

Pure data: dataclasses + the ListingWatcher Protocol. No I/O, no asyncio,
no Telegram. These are the payloads the bot receives from the processor
and the contract the bot depends on.
"""
from barabot.domains.bot.model.events import (
    HitCreated,
    HitEvent,
    HitRemoved,
    HitUpdated,
)
from barabot.domains.bot.model.hit import Hit
from barabot.domains.bot.model.message import Message
from barabot.domains.bot.model.subs import Subscription

__all__ = [
    "Hit",
    "Message",
    "Subscription",
    "HitCreated",
    "HitUpdated",
    "HitRemoved",
    "HitEvent",
]
