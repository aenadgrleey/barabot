"""The user-facing bot domain.

Cross-domain contract (per the project refactor spec):
`domains/bot/*` may import from `barabot.core` and from its own
`model/` and `listwatcher.py`. It MUST NOT import from
`domains/scraper/*` or `domains/processor/*` — the only bridge to
those is the `ListingWatcher` Protocol defined here.

See `README.md` in this directory for the planned scope.
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

__all__ = [
    "Hit",
    "Message",
    "Subscription",
    "HitCreated",
    "HitUpdated",
    "HitRemoved",
    "HitEvent",
    "Client",
]
