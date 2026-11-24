"""Subscription storage. The bot domain owns writes; processing only reads.

Users reach these functions through `barabot.api.BarabotApi`: bot commands,
the agent, MCP and the Mini App. The `python -m barabot subscription` CLI
calls them directly as an admin tool. Blocking; call through
`asyncio.to_thread` from async code.

Chats are stored as the user named them, as a `ChatRef` in `chats` (see
barabot/core/chats.py). Saving a subscription never talks to Telegram:
the scraper resolves new chats, joins them and updates their status on
its own schedule. A chat row stays when no subscription points to it any
more.

`revision` counts changes to the text the decider reads: author,
author_not and query. Changing only the chats keeps it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from barabot.core.chats import ChatId, ChatRef, ChatStatus, parse_chat, ref_from_row, ref_to_row
from barabot.core.db import connect, transaction
from barabot.core.unset import UNSET, Unset


@dataclass(frozen=True)
class SubscriptionChat:
    ref: ChatRef
    chat_id: int | None  # Telegram chat ID once resolved
    title: str | None
    status: ChatStatus


@dataclass(frozen=True)
class SubscriptionRow:
    id: int
    user_id: int
    author: str
    author_not: str | None
    query: str
    active: bool
    revision: int
    chats: tuple[SubscriptionChat, ...]


def clean_text(author: str, query: str, author_not: str | None) -> tuple[str, str, str | None]:
    """Strip the texts; an empty author_not means none."""
    author, query = author.strip(), query.strip()
    if not author or not query:
        raise ValueError("author and query are required")
    return author, query, (author_not or "").strip() or None


def parse_chats(chats: Sequence[str | int]) -> list[ChatRef]:
    """Parse and deduplicate, keeping the user's order."""
    if not chats:
        raise ValueError("a subscription needs at least one chat")
    return list(dict.fromkeys(parse_chat(raw) for raw in chats))


def add(
    db_path: Path,
    *,
    user_id: int,
    author: str,
    query: str,
    chats: Sequence[str | int],
    author_not: str | None = None,
) -> int:
    author, query, author_not = clean_text(author, query, author_not)
    refs = parse_chats(chats)
    conn = connect(db_path)
    try:
        with transaction(conn):
            sub_id = conn.execute(
                "INSERT INTO subscriptions (user_id, author, author_not, query) VALUES (?, ?, ?, ?) RETURNING id",
                (user_id, author, author_not, query),
            ).fetchone()["id"]
            _link_chats(conn, sub_id, refs)
    finally:
        conn.close()
    return int(sub_id)


def get(db_path: Path, subscription_id: int, *, user_id: int) -> SubscriptionRow | None:
    """The user's active subscription, or None if it is missing, removed or someone else's."""
    conn = connect(db_path)
    try:
        rows = _select(conn, "id = ? AND user_id = ? AND active = 1", (subscription_id, user_id))
    finally:
        conn.close()
    return rows[0] if rows else None


def list_all(db_path: Path, *, user_id: int | None = None, active_only: bool = False) -> list[SubscriptionRow]:
    conn = connect(db_path)
    try:
        return _select(conn, "(? IS NULL OR user_id = ?) AND (? = 0 OR active = 1)", (user_id, user_id, active_only))
    finally:
        conn.close()


def update(
    db_path: Path,
    subscription_id: int,
    *,
    user_id: int,
    author: str | Unset = UNSET,
    author_not: str | None | Unset = UNSET,
    query: str | Unset = UNSET,
    chats: Sequence[str | int] | Unset = UNSET,
) -> SubscriptionRow | None:
    """Change the given fields of the user's active subscription; UNSET keeps a field.

    Returns None if the subscription is missing, removed or someone else's.
    """
    refs = None if chats is UNSET else parse_chats(chats)
    conn = connect(db_path)
    try:
        with transaction(conn):
            where = ("id = ? AND user_id = ? AND active = 1", (subscription_id, user_id))
            rows = _select(conn, *where)
            if not rows:
                return None
            current = rows[0]
            new_author, new_query, new_author_not = clean_text(
                current.author if author is UNSET else author,
                current.query if query is UNSET else query,
                current.author_not if author_not is UNSET else author_not,
            )
            if (new_author, new_query, new_author_not) != (current.author, current.query, current.author_not):
                conn.execute(
                    "UPDATE subscriptions SET author = ?, query = ?, author_not = ?, revision = revision + 1 "
                    "WHERE id = ?",
                    (new_author, new_query, new_author_not, subscription_id),
                )
            if refs is not None:
                conn.execute("DELETE FROM subscription_chats WHERE subscription_id = ?", (subscription_id,))
                _link_chats(conn, subscription_id, refs)
            return _select(conn, *where)[0]
    finally:
        conn.close()


def deactivate(db_path: Path, subscription_id: int, *, user_id: int | None = None) -> bool:
    """Turn the subscription off. With user_id, only that user's subscription."""
    conn = connect(db_path)
    try:
        with transaction(conn):
            row = conn.execute(
                "UPDATE subscriptions SET active = 0 WHERE id = ? AND active = 1 AND (? IS NULL OR user_id = ?) "
                "RETURNING id",
                (subscription_id, user_id, user_id),
            ).fetchone()
    finally:
        conn.close()
    return row is not None


def _link_chats(conn: sqlite3.Connection, subscription_id: int, refs: Sequence[ChatRef]) -> None:
    for ref in refs:
        kind, value = ref_to_row(ref)
        chat_id = ref.id if isinstance(ref, ChatId) else None
        conn.execute("INSERT OR IGNORE INTO chats (kind, value, chat_id) VALUES (?, ?, ?)", (kind, value, chat_id))
        conn.execute(
            "INSERT INTO subscription_chats (subscription_id, chat) "
            "SELECT ?, id FROM chats WHERE kind = ? AND value = ?",
            (subscription_id, kind, value),
        )


def _select(conn: sqlite3.Connection, where: str, params: Sequence[object]) -> list[SubscriptionRow]:
    """Subscriptions matching `where`, a condition on `subscriptions` written in this module."""
    subs = conn.execute(
        f"SELECT id, user_id, author, author_not, query, active, revision FROM subscriptions WHERE {where} ORDER BY id",
        params,
    ).fetchall()
    if not subs:
        return []
    chats: dict[int, list[SubscriptionChat]] = {row["id"]: [] for row in subs}
    marks = ",".join("?" * len(chats))
    for row in conn.execute(
        "SELECT sc.subscription_id, c.kind, c.value, c.chat_id, c.title, c.status FROM subscription_chats sc "
        f"JOIN chats c ON c.id = sc.chat WHERE sc.subscription_id IN ({marks}) ORDER BY c.id",
        list(chats),
    ):
        chats[row["subscription_id"]].append(
            SubscriptionChat(
                ref_from_row(row["kind"], row["value"]), row["chat_id"], row["title"], ChatStatus(row["status"])
            )
        )
    return [
        SubscriptionRow(
            id=row["id"],
            user_id=row["user_id"],
            author=row["author"],
            author_not=row["author_not"],
            query=row["query"],
            active=bool(row["active"]),
            revision=row["revision"],
            chats=tuple(chats[row["id"]]),
        )
        for row in subs
    ]
