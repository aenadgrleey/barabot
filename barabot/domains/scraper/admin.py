"""One-off scraper commands: join chats and dump recent messages.

Through tgmux, joining from the barabot integration also makes the gateway
assign the chat to barabot, which is what lets the scraper read it. tgmux
allows one connection per integration, so stop barabot.service first.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from telethon import errors, functions, utils

from barabot.core.config import Config
from barabot.domains.scraper.service import connect_client, make_client, to_seen


def _kind(entity) -> str:
    if getattr(entity, "forum", False):
        return "forum supergroup"
    if getattr(entity, "megagroup", False):
        return "supergroup"
    if getattr(entity, "broadcast", False):
        return "channel"
    return type(entity).__name__


async def join(cfg: Config, targets: Sequence[str]) -> list[dict]:
    """Join each chat (by @username or t.me link) and return its marked ID for WATCH_CHATS."""
    client = make_client(cfg)
    await connect_client(cfg, client)
    joined = []
    try:
        for target in targets:
            entity = await client.get_entity(target.removeprefix("https://t.me/"))
            try:
                await client(functions.channels.JoinChannelRequest(entity))
                status = "joined"
            except errors.UserAlreadyParticipantError:
                status = "already a member"
            except errors.InviteRequestSentError:
                status = "join request sent, waiting for admin approval"
            joined.append({
                "target": target,
                "id": utils.get_peer_id(entity),
                "title": getattr(entity, "title", ""),
                "kind": _kind(entity),
                "status": status,
            })
    finally:
        await client.disconnect()
    return joined


async def sample(cfg: Config, chat: str | int, limit: int, out: Path) -> int:
    """Write the last `limit` messages of a chat as JSON lines, to design subscriptions and benches."""
    client = make_client(cfg)
    await connect_client(cfg, client)
    written = 0
    try:
        entity = await client.get_entity(chat)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as stream:
            async for msg in client.iter_messages(entity, limit=limit):
                seen = to_seen(msg)
                if seen is None:
                    continue
                stream.write(json.dumps({
                    "chat_id": seen.chat_id,
                    "msg_id": seen.msg_id,
                    "grouped_id": seen.grouped_id,
                    "date": seen.date.isoformat(),
                    "text": seen.text,
                    "media": seen.media,
                }, ensure_ascii=False) + "\n")
                written += 1
    finally:
        await client.disconnect()
    return written
