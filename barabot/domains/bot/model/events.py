# barabot/domains/bot/model/events.py
"""Outbox events the bot consumes from the processor.

The processor writes one row per change into `outbox_events`. The bot
polls the table, renders the change to a Telegram user, then acks the
row. The three shapes — Created / Updated / Removed — are flattened
into a single `HitEvent` union so the bot can process a batch in one
pass without juggling three separate queries.
"""
from __future__ import annotations

from dataclasses import dataclass

from barabot.domains.bot.model.hit import Hit


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
