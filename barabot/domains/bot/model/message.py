# barabot/domains/bot/model/message.py
"""Full description of a chat message.

Single payload for everything that describes a message: text, sender,
media_count. `ListingWatcher.get_message` returns `Message`, any future
read-side handle will too — one representation of "what was written
there" across the bot.
"""
from __future__ import annotations

from dataclasses import dataclass


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