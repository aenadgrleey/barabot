"""Periodic digest of processing, logged at NOTICE so it reaches Telegram.

Runs as a service next to the scraper and the bot. Every interval it
reports what happened since the previous digest: messages stored,
listings decided, Jev requests (and how many carried photos), tokens and
spend, then each user the spend was charged to, with their
subscriptions, chats and matches.

Spend comes from the injected `Budget`; messages, matches, subscriptions
and chats it reads from the app database itself.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from barabot.core.chats import ref_from_row
from barabot.core.db import connect
from barabot.core.logs import notice
from barabot.domains.budget import START, Budget, Charge, Mark, to_usd

log = logging.getLogger(__name__)

SUBSCRIPTION_TEXT = 40  # characters of author and query shown per subscription


@dataclass(frozen=True)
class _Marks:
    """Where each log ends; rowids, not timestamps: datetime('now') has one-second resolution."""

    message_rowid: int
    match_rowid: int
    budget: Mark


_FIRST = _Marks(0, 0, START)


class StatsReporter:
    def __init__(self, db_path: Path, budget: Budget, *, interval_seconds: float) -> None:
        self._db_path = db_path
        self._budget = budget
        self._interval = interval_seconds
        self._since: _Marks | None = None

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

    def _marks(self) -> _Marks:
        conn = connect(self._db_path)
        try:
            messages = conn.execute("SELECT coalesce(max(rowid), 0) FROM messages").fetchone()[0]
            matches = conn.execute("SELECT coalesce(max(rowid), 0) FROM matches").fetchone()[0]
        finally:
            conn.close()
        return _Marks(messages, matches, self._budget.mark())

    def report(self) -> str:
        since = self._since or _FIRST
        now = self._marks()
        jev = self._budget.jev_requests(since.budget, now.budget)
        charges = self._budget.charges(since.budget, now.budget)
        conn = connect(self._db_path)
        try:
            messages = conn.execute(
                "SELECT count(*) FROM messages WHERE rowid > ? AND rowid <= ?",
                (since.message_rowid, now.message_rowid),
            ).fetchone()[0]
            decided = conn.execute(
                "SELECT count(DISTINCT chat_id || ':' || album || ':' || ref || ':' || version) AS listings, "
                "coalesce(sum(matched), 0) AS matches FROM matches WHERE rowid > ? AND rowid <= ?",
                (since.match_rowid, now.match_rowid),
            ).fetchone()
            users = _user_lines(conn, charges, since, now)
        finally:
            conn.close()
        self._since = now
        spent = self._budget.jev_spent_usd()
        limit = self._budget.jev_limit_usd
        budget = f"${spent:.3f} of ${limit:.2f}" if limit else f"${spent:.3f}, no limit"
        avg = jev.input_tokens // jev.requests if jev.requests else 0
        text = (
            f"Digest: {messages} new message(s), {decided['listings']} listing(s) checked, "
            f"{decided['matches']} match(es); Jev {jev.requests} request(s), "
            f"{jev.photo_requests} with photos ({jev.photos} photos), "
            f"{jev.input_tokens} tokens (~{avg}/request, ${to_usd(jev.nano_usd):.4f}); "
            f"spent {budget}"
        )
        # One journal line per user and per subscription: aenaut-notify cuts a single line at 700 characters.
        text = "\n".join([text, *users])
        notice(log, "%s", text)
        return text


def _user_lines(conn: sqlite3.Connection, charges: Sequence[Charge], since: _Marks, now: _Marks) -> list[str]:
    """Who the period's spend was charged to: each user, then each of their subscriptions with its chats.

    A subscription is listed when the period charged it; its matches are the ones decided in the period.
    """
    if not charges:
        return []
    ids = [c.subscription_id for c in charges if c.subscription_id is not None]
    marks = ",".join("?" * len(ids))
    subs = {
        row["id"]: row
        for row in conn.execute(f"SELECT id, author, query, active FROM subscriptions WHERE id IN ({marks})", ids)
    }
    matches = dict(
        conn.execute(
            "SELECT subscription_id, sum(matched) FROM matches WHERE rowid > ? AND rowid <= ? GROUP BY subscription_id",
            (since.match_rowid, now.match_rowid),
        ).fetchall()
    )
    chats: dict[int, list[str]] = {}
    for row in conn.execute(
        "SELECT sc.subscription_id, c.kind, c.value, c.username, c.title FROM subscription_chats sc "
        f"JOIN chats c ON c.id = sc.chat WHERE sc.subscription_id IN ({marks}) ORDER BY c.id",
        ids,
    ):
        chats.setdefault(row["subscription_id"], []).append(_chat_label(row))

    by_user: dict[int | None, list[Charge]] = {}
    for charge in charges:
        by_user.setdefault(charge.user_id, []).append(charge)
    lines = []
    for user_id, own in sorted(by_user.items(), key=lambda item: (-sum(c.nano_usd for c in item[1]), str(item[0]))):
        who = f"user {user_id}" if user_id is not None else "no user"
        lines.append(f"{who}: ${to_usd(sum(c.nano_usd for c in own)):.4f}, {len(own)} subscription(s)")
        for charge in sorted(own, key=lambda c: (-c.nano_usd, c.subscription_id or 0)):
            sub_id = charge.subscription_id
            label = f"#{sub_id}" if sub_id is not None else "no subscription"
            head = f"  {label} ${to_usd(charge.nano_usd):.4f}, {charge.requests} request(s), "
            head += f"{matches.get(sub_id, 0)} match(es)"
            sub = subs.get(sub_id) if sub_id is not None else None
            if sub is None:
                lines.append(head if sub_id is None else head + ": subscription is gone")
                continue
            off = " (off)" if not sub["active"] else ""
            # dict.fromkeys: @name and the marked ID of one chat show the same label
            where = ", ".join(dict.fromkeys(chats.get(sub["id"], []))) or "no chats"
            lines.append(f'{head}{off}: "{_short(sub["author"])}" / "{_short(sub["query"])}" in {where}')
    return lines


def _chat_label(row: sqlite3.Row) -> str:
    """@username, else the title, else the chat as the user named it."""
    if row["username"]:
        return f"@{row['username']}"
    if row["title"]:
        return row["title"]
    return str(ref_from_row(row["kind"], row["value"]))


def _short(text: str, limit: int = SUBSCRIPTION_TEXT) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
