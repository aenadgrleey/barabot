"""Message processing pipeline — extracted from Bot._handle_new_or_edit so
backfill and live-handlers share the exact same logic (embed → dedup → upsert).

The function takes a normalized `IngestMessage` (just text + chat_id + msg_id
+ sender), not a Telethon Message, so it doesn't depend on Telegram types and
is trivial to test.

Result of the dedup decision is a `ProcessResult` enum so callers can log or
route it differently (live handler logs to console; backfill aggregates counts).
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .db import iter_embeddings, upsert
from .embed import embed
from .similarity import best_match

log = logging.getLogger("barabot.processor")


class ProcessResult(str, Enum):
    NEW = "new"
    DUP = "dup"
    SKIPPED_EMPTY = "skipped_empty"
    SKIPPED_SERVICE = "skipped_service"
    EMBED_FAILED = "embed_failed"
    DB_FAILED = "db_failed"


@dataclass(frozen=True)
class IngestMessage:
    """Normalized message input — independent of Telethon/Pyrogram types.

    `photo_count` is the number of media items in the album this message
    belongs to. For a single photo (no album) it's 1. For a Telegram media
    group of, say, 4 photos with one shared caption, this is 4 — and the
    `text` is the shared caption (taken from whichever Message carried it).
    """
    chat_id: int
    msg_id: int
    text: str
    sender: str | None
    is_service: bool = False
    photo_count: int = 1


@dataclass(frozen=True)
class ProcessOutcome:
    """What happened to this message — used by both live and backfill paths."""
    result: ProcessResult
    score: float = 0.0
    matched_msg_id: int | None = None
    error: str | None = None


async def process_message(
    msg: IngestMessage,
    *,
    conn,
    cfg,
    semaphore: asyncio.Semaphore,
) -> ProcessOutcome:
    """Apply the full barabot pipeline to a single message.

    Order:
    1. Filter service / empty text (cheap, do first)
    2. Embed (slow, rate-limit via semaphore)
    3. Linear scan vs all stored embeddings in this chat
    4. If best_score ≥ threshold → DUP, do NOT upsert
    5. Else → NEW, upsert (chat_id, msg_id, text, embedding, sender, ts)

    DB and embed errors are caught and surfaced as ProcessOutcome rather than
    raised, so a single bad message doesn't kill the whole bot.
    """
    if msg.is_service:
        return ProcessOutcome(result=ProcessResult.SKIPPED_SERVICE)
    text = msg.text.strip()
    if not text:
        return ProcessOutcome(result=ProcessResult.SKIPPED_EMPTY)

    try:
        vector = await _embed(text, cfg, semaphore)
    except Exception as exc:  # noqa: BLE001 — must not kill the bot
        log.error("embed failed for msg %s: %s", msg.msg_id, exc)
        return ProcessOutcome(result=ProcessResult.EMBED_FAILED, error=str(exc))

    candidates = await iter_embeddings(conn, msg.chat_id)
    best_id, best_score = best_match(vector, candidates)

    if best_score >= cfg.similarity_threshold:
        log.info(
            "dup: chat=%s msg=%s score=%.3f (matches msg %s)",
            msg.chat_id, msg.msg_id, best_score, best_id,
        )
        return ProcessOutcome(
            result=ProcessResult.DUP,
            score=best_score,
            matched_msg_id=best_id,
        )

    log.info(
        "NEW: chat=%s msg=%s score=%.3f text=%r",
        msg.chat_id, msg.msg_id, best_score, text[:200],
    )
    try:
        await upsert(
            conn,
            chat_id=msg.chat_id,
            msg_id=msg.msg_id,
            text=text,
            embedding=vector,
            sender=msg.sender,
            created_at=datetime.now(timezone.utc).isoformat(),
            photo_count=msg.photo_count,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("db upsert failed for msg %s: %s", msg.msg_id, exc)
        return ProcessOutcome(result=ProcessResult.DB_FAILED, error=str(exc), score=best_score)

    return ProcessOutcome(result=ProcessResult.NEW, score=best_score)


async def _embed(text: str, cfg, semaphore: asyncio.Semaphore) -> list[float]:
    async with semaphore:
        return await asyncio.to_thread(
            embed,
            text,
            api_key=cfg.openai_api_key,
            model=cfg.embedding_model,
            url=cfg.embedding_url,
            retries=cfg.embedding_retries,
            timeout=cfg.embedding_timeout,
        )