"""Media-group (album) coalescing for Telegram messages.

In Telegram, when a user sends several photos together as an album, the
client actually emits several Message objects with the same `grouped_id`.
Only one of them carries the caption (text); the others have `text=None`.

If you iterate `iter_messages` flatly and skip empty-text messages (which is
what we want for single-message noise), you lose entire albums — the caption
attached to the album is dropped because it's stuck on a Message with
non-empty text, but the OTHER photos in the same album still come through
with empty text and get skipped, *and* the non-empty caption is then
deduped against them as if it were a single-photo listing. Result: we keep
the caption but lose the album structure, and we waste an embed per photo.

This module fixes that: given a flat list (or async iterator) of messages,
yield one synthetic "unified" record per media group, with:
  - the caption (from whichever Message carried it, if any)
  - the lowest msg_id of the group (so the dedup PK is stable)
  - the photo_count = number of Messages in the group

The output is independent of Telethon types: it produces simple
`UnifiedItem` dataclasses that backfill.py and bot.py both consume.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

from telethon.tl.custom.message import Message as TLMessage
from telethon.tl.types import MessageService


@dataclass(frozen=True)
class UnifiedItem:
    """One deduplicated ingestable thing — either a single message or a whole album."""
    chat_id: int
    msg_id: int            # PK carrier: lowest msg_id in the group (or the singleton's id)
    text: str              # shared caption, or '' if no caption anywhere in the group
    sender: str | None
    is_service: bool
    photo_count: int       # 1 for singletons; len(group) for albums


def _is_service(msg) -> bool:
    # MessageService subclasses don't have .service; the type check is canonical.
    return isinstance(msg, MessageService) or bool(getattr(msg, "service", False))


def _msg_text(msg) -> str:
    return (getattr(msg, "text", "") or getattr(msg, "caption", "") or "")


def _msg_sender(msg) -> str | None:
    sender = getattr(msg, "sender", None)
    if sender is None:
        return None
    uname = getattr(sender, "username", None)
    if uname:
        return f"@{uname}"
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    full = (first + " " + last).strip()
    return full or None


def coalesce(messages: Iterable[TLMessage], chat_id: int) -> list[UnifiedItem]:
    """Take a flat list of Messages and yield UnifiedItems, one per media group.

    Order is preserved: we walk the input in order and emit either a singleton
    UnifiedItem (msg has no grouped_id) or a single UnifiedItem per group
    (emitted at the position of the FIRST msg in the group we encounter).
    Subsequent messages in the same group are consumed but not re-emitted.

    A group with NO caption across any of its messages is dropped entirely
    (returns [] for that group) — same semantics as skipping empty singletons.
    """
    out: list[UnifiedItem] = []
    seen_groups: dict[int, int] = {}  # grouped_id -> index in `out`

    for msg in messages:
        gid = getattr(msg, "grouped_id", None)
        if gid is None:
            # Singleton — no grouping, just normalize.
            if _is_service(msg):
                out.append(UnifiedItem(
                    chat_id=chat_id,
                    msg_id=msg.id,
                    text="",
                    sender=_msg_sender(msg),
                    is_service=True,
                    photo_count=1,
                ))
                continue
            text = _msg_text(msg).strip()
            if not text:
                # Drop noise (stickers, voice notes, etc. with no caption).
                continue
            out.append(UnifiedItem(
                chat_id=chat_id,
                msg_id=msg.id,
                text=text,
                sender=_msg_sender(msg),
                is_service=False,
                photo_count=1,
            ))
            continue

        # Grouped message — check if we already started a UnifiedItem for this group.
        if gid in seen_groups:
            idx = seen_groups[gid]
            existing = out[idx]
            # If existing had no text, this message might be the caption carrier.
            if not existing.text:
                text = _msg_text(msg).strip()
                if text:
                    out[idx] = UnifiedItem(
                        chat_id=existing.chat_id,
                        msg_id=existing.msg_id,
                        text=text,
                        sender=existing.sender or _msg_sender(msg),
                        is_service=existing.is_service,
                        photo_count=existing.photo_count,  # updated below
                    )
            # Always bump photo_count, since we just confirmed another photo.
            out[idx] = UnifiedItem(
                chat_id=existing.chat_id,
                msg_id=existing.msg_id,
                text=out[idx].text,  # already updated above if needed
                sender=out[idx].sender,
                is_service=out[idx].is_service,
                photo_count=existing.photo_count + 1,
            )
            continue

        # First message of a new group — open the UnifiedItem, slot will be filled.
        if _is_service(msg):
            # Service messages shouldn't usually have grouped_id, but defensive.
            continue
        text = _msg_text(msg).strip()
        # Use the lowest msg_id as PK carrier (msg.id is fine here because this
        # is the first one we've seen; later members can't have lower IDs since
        # they're ordered by Telegram emission).
        out.append(UnifiedItem(
            chat_id=chat_id,
            msg_id=msg.id,
            text=text,
            sender=_msg_sender(msg),
            is_service=False,
            photo_count=1,  # will be incremented as more group members arrive
        ))
        seen_groups[gid] = len(out) - 1

    # Drop any group whose caption was never filled in.
    return [u for u in out if u.text or u.is_service]