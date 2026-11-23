"""Telegram updates reduced to what ingestion stores.

Live events and backfill pages both convert into these, so the ingest
path does not depend on Telethon types.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class SeenMessage:
    """A new or edited message."""

    chat_id: int
    msg_id: int
    grouped_id: int | None
    sender_id: int | None
    date: datetime
    text: str
    media: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class DeletedMessages:
    """`chat_id` is None for basic groups and private chats."""

    chat_id: int | None
    msg_ids: list[int]
