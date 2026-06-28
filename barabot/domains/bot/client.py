# barabot/domains/bot/listwatcher.py
"""Contract the bot depends on: any backend that matches listings.

The bot never imports from `domains/processor/*` directly. It only
sees this Protocol. The processor implements it; the bot types against
it; a fake/mock for tests does too. If the backend's storage layout
changes tomorrow, only the implementation moves.
"""

from __future__ import annotations

from typing import Protocol

from barabot.domains.bot.model.events import HitEvent
from barabot.domains.bot.model.message import Message
from barabot.domains.bot.model.subs import Subscription


class Client(Protocol):
    """Контракт backend'а поиска объявлений.

    Multi-tenant: данные per user_id, но poll/ack — глобальные,
    ботфасад один на всех юзеров.

    Жизненный цикл события:
      backend → INSERT в outbox_events (Created/Updated/Removed)
      bot     → poll_events()           забирает всю накопленную пачку
      bot     → шлёт юзерам, применяет правила
      bot     → ack(event_ids=[...])    подтверждает доставку
      backend → удаляет подтверждённые строки
    """

    async def subscribe(
        self,
        *,
        user_id: int,
        query: str,
        chat_ids: list[int] | None = None,
        min_score: float = 0.0,
    ) -> Subscription: ...

    async def unsubscribe(
        self,
        *,
        user_id: int,
        subscription_id: int,
    ) -> bool: ...

    async def list_subscriptions(
        self,
        *,
        user_id: int,
    ) -> list[Subscription]: ...

    async def poll_events(
        self,
        *,
        limit: int = 50,
    ) -> list[HitEvent]: ...

    async def ack(
        self,
        *,
        event_ids: list[int],
    ) -> int: ...  # сколько строк реально удалено

    async def get_message(
        self,
        *,
        chat_id: int,
        msg_id: int,
    ) -> Message | None: ...
