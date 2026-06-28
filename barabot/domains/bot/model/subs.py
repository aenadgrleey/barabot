# barabot/domains/bot/model/subs.py
"""User subscription: a semantic query scoped to one or more watched chats."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Subscription:
    """Подписка конкретного пользователя на семантический запрос."""
    id: int
    user_id: int
    query: str
    chat_ids: list[int] | None = None   # None = все watched
    min_score: float = 0.0
    created_at: int = 0
