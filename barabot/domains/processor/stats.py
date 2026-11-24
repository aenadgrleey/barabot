"""Periodic digest of processing, logged at NOTICE so it reaches Telegram.

Runs as a service next to the scraper and the bot. Every interval it reads
the app database and reports what happened since the previous digest:
messages stored, listings decided, Jev requests (and how many carried
photos), tokens, spend, matches.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from barabot.core.db import connect
from barabot.core.logs import notice
from barabot.domains.processor.budget import Budget

log = logging.getLogger(__name__)


class StatsReporter:
    def __init__(self, db_path: Path, budget: Budget, *, interval_seconds: float) -> None:
        self._db_path = db_path
        self._budget = budget
        self._interval = interval_seconds
        self._since: dict | None = None

    async def start(self) -> None:
        self._since = await asyncio.to_thread(self._marks)

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except TimeoutError:
                try:
                    await asyncio.to_thread(self.report)
                except Exception:
                    log.exception("Could not build the digest")

    async def stop(self) -> None:
        pass

    def _marks(self) -> dict:
        conn = connect(self._db_path)
        try:
            return {
                "message_rowid": conn.execute("SELECT coalesce(max(rowid), 0) FROM messages").fetchone()[0],
                "usage_id": conn.execute("SELECT coalesce(max(id), 0) FROM jev_usage").fetchone()[0],
                # rowids, not timestamps: datetime('now') has one-second resolution
                "match_rowid": conn.execute("SELECT coalesce(max(rowid), 0) FROM matches").fetchone()[0],
            }
        finally:
            conn.close()

    def report(self) -> str:
        since = self._since or {"message_rowid": 0, "usage_id": 0, "match_rowid": 0}
        now = self._marks()
        conn = connect(self._db_path)
        try:
            messages = conn.execute(
                "SELECT count(*) FROM messages WHERE rowid > ?", (since["message_rowid"],)
            ).fetchone()[0]
            usage = conn.execute(
                "SELECT count(*) AS requests, coalesce(sum(round = 2), 0) AS photo_requests, "
                "coalesce(sum(photos), 0) AS photos, coalesce(sum(input_tokens), 0) AS tokens "
                "FROM jev_usage WHERE id > ? AND id <= ?",
                (since["usage_id"], now["usage_id"]),
            ).fetchone()
            per_subscription = conn.execute(
                "SELECT subscription_id, count(*) FROM matches WHERE matched = 1 AND rowid > ? AND rowid <= ? "
                "GROUP BY subscription_id ORDER BY count(*) DESC",
                (since["match_rowid"], now["match_rowid"]),
            ).fetchall()
            decided = conn.execute(
                "SELECT count(DISTINCT chat_id || ':' || album || ':' || ref || ':' || version) AS listings, "
                "coalesce(sum(matched), 0) AS matches FROM matches WHERE rowid > ? AND rowid <= ?",
                (since["match_rowid"], now["match_rowid"]),
            ).fetchone()
        finally:
            conn.close()
        self._since = now
        spent = self._budget.spent_usd()
        limit = self._budget.limit_usd
        budget = f"${spent:.3f} of ${limit:.2f}" if limit else f"${spent:.3f}, no limit"
        avg = usage["tokens"] // usage["requests"] if usage["requests"] else 0
        text = (
            f"Digest: {messages} new message(s), {decided['listings']} listing(s) checked, "
            f"{decided['matches']} match(es); Jev {usage['requests']} request(s), "
            f"{usage['photo_requests']} with photos ({usage['photos']} photos), "
            f"{usage['tokens']} tokens (~{avg}/request, ${self._budget.cost(usage['tokens']):.4f}); "
            f"spent {budget}"
        )
        if per_subscription:
            text += "; matches by subscription: " + ", ".join(f"#{sid}×{n}" for sid, n in per_subscription)
        notice(log, "%s", text)
        return text
