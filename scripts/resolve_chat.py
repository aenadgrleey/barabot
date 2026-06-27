#!/usr/bin/env python3
"""One-shot: resolve a public Telegram chat handle to its numeric chat_id.

Reads SESSION_STRING from /root/projects/barabot/.env. Writes the chat_id to
stdout in a parseable form (key=value lines).

Usage:
    .venv/bin/python scripts/resolve_chat.py @NSbaraholka
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetFullChannelRequest
from telethon.tl.types import Channel


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"


def _read_env() -> dict[str, str]:
    """Minimal .env reader (we don't need python-dotenv for a one-shot script)."""
    out: dict[str, str] = {}
    if not ENV_PATH.exists():
        sys.exit(f"ERROR: {ENV_PATH} does not exist")
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


async def main() -> int:
    if len(sys.argv) < 2:
        sys.exit("usage: resolve_chat.py @handle")
    handle = sys.argv[1].lstrip("@")

    env = _read_env()
    api_id = int(env["API_ID"])
    api_hash = env["API_HASH"]
    session = env["SESSION_STRING"]

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            sys.exit("ERROR: session not authorized")

        entity = await client.get_entity(handle)
        if not isinstance(entity, Channel):
            sys.exit(f"ERROR: @{handle} is {type(entity).__name__}, expected Channel")

        full = await client(GetFullChannelRequest(entity))
        print(f"chat_id={entity.id}")
        print(f"title={entity.title}")
        print(f"username=@{entity.username}" if entity.username else "username=<none>")
        print(f"participants={full.full_chat.participants_count}")
        return 0
    finally:
        await client.disconnect()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))