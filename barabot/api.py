# barabot/api.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


# ─── Payloads ────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Hit:
    """Матч: указатель на объявление в чате.

    Худой payload — только метаданные матча. Сам текст и автор живут в
    Message, бот достаёт его через get_message когда хочет показать.
    Это держит outbox_events лёгким: ничего тяжёлого в очереди.
    """
    id: int
    chat_id: int
    msg_id: int
    created_at: int              # когда объявление появилось в чате
    matched_at: int              # когда движок зафиксировал матч
    score: float                 # cosine similarity [-1, 1]
    user_id: int                 # кому показать
    subscription_id: int         # какая подписка сработала (на будущее)
    rerank_score: float | None = None


@dataclass(frozen=True, slots=True)
class Message:
    """Полное описание сообщения из чата.

    Единый payload для всего что про сообщение: text, sender, media_count.
    get_message возвращает Message, любая будущая ручка — тоже Message.
    У фронта одно представление «что там было написано».
    """
    chat_id: int
    msg_id: int
    text: str
    sender: str | None
    created_at: int
    media_count: int = 0


@dataclass(frozen=True, slots=True)
class Subscription:
    """Подписка конкретного пользователя на семантический запрос."""
    id: int
    user_id: int
    query: str
    chat_ids: list[int] | None = None   # None = все watched
    min_score: float = 0.0
    created_at: int = 0


# ─── Events ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class HitCreated:
    """Новый матч: нашлось объявление, раньше такого не было."""
    event_id: int
    event_at: int
    hit: Hit


@dataclass(frozen=True, slots=True)
class HitUpdated:
    """Матч обновился: edit сообщения, пересчёт score и т.п.

    changed — список имён полей которые поменялись:
      "score", "rerank_score"
    Бот решает, реагировать ли (переслать юзеру или молча обновить).
    """
    event_id: int
    event_at: int
    hit: Hit
    changed: list[str]


@dataclass(frozen=True, slots=True)
class HitRemoved:
    """Матч больше не актуален.

    reason — почему:
      "deleted"              — сообщение удалено в чате
      "score_dropped"        — после edit score упал ниже min_score
      "subscription_removed" — юзер удалил подписку
      "manual"               — оператор нажал кнопку

    hit — последнее известное состояние матча, чтобы фронт мог
    понять контекст без похода в бэкенд (например, чтобы редактировать
    своё прошлое сообщение «❌ удалено» или просто игнорировать).
    """
    event_id: int
    event_at: int
    hit: Hit
    reason: str


HitEvent = HitCreated | HitUpdated | HitRemoved


# ─── Contract ────────────────────────────────────────────────────────────────

class ListingWatcher(Protocol):
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
    ) -> int: ...   # сколько строк реально удалено

    async def get_message(
        self,
        *,
        chat_id: int,
        msg_id: int,
    ) -> Message | None: ...