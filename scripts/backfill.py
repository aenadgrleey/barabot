#!/usr/bin/env python3
"""Backfill a watch-list chat from history through the same processing
pipeline as the live bot. Uses barabot.processor.process_message so dedup,
embed, and upsert logic is byte-identical between live and backfill.

Usage:
    .venv/bin/python scripts/backfill.py --chat 1570959321 --limit 200
    .venv/bin/python scripts/backfill.py --chat 1570959321 --limit 0  # all history
    .venv/bin/python scripts/backfill.py --handle @NSbaraholka --limit 200
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from barabot.config import load as load_cfg
from barabot.db import connect
from barabot.media_group import coalesce
from barabot.processor import (
    IngestMessage,
    ProcessResult,
    process_message,
)

log = logging.getLogger("barabot.backfill")

ENV_PATH = Path(__file__).resolve().parents[1] / ".env"

# Rate-limit: OpenRouter's free tier allows ~60 req/min. With concurrency=4
# we'd hit it instantly. We serialize embed requests (semaphore=1) for
# backfill so we stay under the limit, and sleep between to be polite.
EMBED_SEMAPHORE_BACKFILL = 1
SLEEP_BETWEEN_MSG_S = 0.4  # 2.5 req/sec → safe under 60/min


# (resolve_handle removed — replaced with inline resolution in main_async
# because iter_messages needs the full TL entity, not just an id)


async def main_async(chat_target: str, limit: int, dry_run: bool) -> int:
    cfg = load_cfg()

    # Use the LIVE bot's SQLite. WAL mode + busy_timeout so we coexist with
    # the running barabot process without SQLITE_BUSY.
    conn = connect(cfg.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    # Auto-detect API_ID / API_HASH / SESSION_STRING from .env
    env = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip()

    api_id = int(env["API_ID"])
    api_hash = env["API_HASH"]
    session = env["SESSION_STRING"]

    client = TelegramClient(StringSession(session), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        sys.exit("ERROR: session not authorized")
    try:
        # Resolve chat_id but also keep the entity object — iter_messages on a
        # bare positive int for a Channel/supergroup raises PeerUser error.
        if chat_target.startswith("@"):
            chat_entity = await client.get_entity(chat_target)
            chat_id = chat_entity.id
        else:
            try:
                chat_entity = await client.get_entity(int(chat_target))
                chat_id = chat_entity.id
            except Exception:
                chat_entity = int(chat_target)
                chat_id = int(chat_target)

        if limit <= 0:
            iter_kwargs: dict = {"reverse": True}
        else:
            iter_kwargs = {"limit": limit, "reverse": True}

        # Snapshot before we start so we can report "what was already in DB"
        before_ids = {
            row[0]
            for row in conn.execute(
                "SELECT msg_id FROM items WHERE chat_id = ?", (chat_id,)
            ).fetchall()
        }
        log.info(
            "backfill start: chat=%s limit=%s dry_run=%s existing_rows=%d",
            chat_id, limit, dry_run, len(before_ids),
        )

        semaphore = asyncio.Semaphore(EMBED_SEMAPHORE_BACKFILL)
        outcome_counts: Counter = Counter()
        fetched = 0
        unified_count = 0
        albums_collapsed = 0
        skipped_already_in_db = 0
        first_msg_id = None
        last_msg_id = None
        t0 = time.monotonic()

        # PHASE 1: pull all messages into memory. Telegram sends albums as
        # multiple Messages sharing a `grouped_id`; we need to see them all
        # together to coalesce. iter_messages is bounded by `limit` so
        # memory is bounded too.
        raw_messages = []
        async for msg in client.iter_messages(chat_entity, **iter_kwargs):
            raw_messages.append(msg)
        fetched = len(raw_messages)
        if fetched:
            first_msg_id = raw_messages[0].id
            last_msg_id = raw_messages[-1].id

        # PHASE 2: coalesce media groups → UnifiedItem list
        unified_items = coalesce(raw_messages, chat_id)
        unified_count = len(unified_items)
        albums_collapsed = fetched - unified_count  # how many Messages got merged

        log.info(
            "coalesce: raw_messages=%d unified_items=%d (collapsed %d photos into albums)",
            fetched, unified_count, albums_collapsed,
        )

        # PHASE 3: process each UnifiedItem through the shared pipeline
        for item in unified_items:
            if item.msg_id in before_ids:
                skipped_already_in_db += 1
                continue

            if dry_run:
                if item.is_service:
                    outcome_counts[ProcessResult.SKIPPED_SERVICE] += 1
                    continue
                outcome_counts[
                    ProcessResult.NEW if item.text else ProcessResult.SKIPPED_EMPTY
                ] += 1
                continue

            ingest = IngestMessage(
                chat_id=item.chat_id,
                msg_id=item.msg_id,
                text=item.text,
                sender=item.sender,
                is_service=item.is_service,
                photo_count=item.photo_count,
            )
            outcome = await process_message(
                ingest, conn=conn, cfg=cfg, semaphore=semaphore,
            )
            outcome_counts[outcome.result] += 1

            # Rate-limit OpenRouter: serialize via semaphore, then nap
            await asyncio.sleep(SLEEP_BETWEEN_MSG_S)

            # Progress every 20 unified items
            if unified_count and (unified_count % 20 == 0):
                log.info(
                    "progress: unified=%d new=%d dup=%d embed_fail=%d last_msg=%s",
                    unified_count,
                    outcome_counts[ProcessResult.NEW],
                    outcome_counts[ProcessResult.DUP],
                    outcome_counts[ProcessResult.EMBED_FAILED],
                    item.msg_id,
                )

        elapsed = time.monotonic() - t0
        # Force WAL checkpoint so reporter sees the new rows even if live bot
        # is idle. connect() uses synchronous sqlite3; safe to do from async.
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

        total_after = conn.execute(
            "SELECT COUNT(*) FROM items WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]
        added = total_after - len(before_ids)

        report = {
            "chat_id": chat_id,
            "limit": limit,
            "dry_run": dry_run,
            "elapsed_s": round(elapsed, 1),
            "fetched_raw_messages": fetched,
            "unified_items": unified_count,
            "albums_collapsed": albums_collapsed,
            "first_msg_id": first_msg_id,
            "last_msg_id": last_msg_id,
            "skipped_already_in_db": skipped_already_in_db,
            "added_to_db": added,
            "outcomes": dict(outcome_counts),
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    finally:
        await client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill a baraholka chat into barabot.sqlite")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--chat", help="Numeric chat_id (e.g. 1570959321)")
    g.add_argument("--handle", help="Public @username (e.g. @NSbaraholka)")
    parser.add_argument(
        "--limit", type=int, default=200,
        help="Number of messages to fetch (0 = all history). Default 200.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip embed + upsert; just count what would be processed.",
    )
    parser.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)

    target = args.chat or args.handle
    return asyncio.run(main_async(target, args.limit, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())