# barabot/domains/bot/model/hit.py
"""Match payload + the chat message it points at.

`Hit` is the slim record the processor writes into `outbox_events`:
just enough metadata to render a notification and to follow the
pointer back to the source message. The body itself lives in
`Message`, fetched on demand via `ListingWatcher.get_message` so the
outbox stays light.
"""
from __future__ import annotations

from dataclasses import dataclass


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
