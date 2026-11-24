"""Subscription writes. The bot domain owns them; processing only reads.

Command mode, agent mode and the Mini App will call these functions. Until
the bot exists, `python -m barabot subscription ...` does. Blocking; call
through `asyncio.to_thread` from async code.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from barabot.core.db import connect, transaction


@dataclass(frozen=True)
class SubscriptionRow:
    id: int
    user_id: int
    author: str
    author_not: str | None
    query: str
    active: bool
    chats: tuple[int, ...]


def add(
    db_path: Path,
    *,
    user_id: int,
    author: str,
    query: str,
    chats: Sequence[int],
    author_not: str | None = None,
) -> int:
    author, query = author.strip(), query.strip()
    author_not = (author_not or "").strip() or None
    if not author or not query:
        raise ValueError("author and query are required")
    if not chats:
        raise ValueError("a subscription needs at least one chat")
    conn = connect(db_path)
    try:
        with transaction(conn):
            sub_id = conn.execute(
                "INSERT INTO subscriptions (user_id, author, author_not, query) VALUES (?, ?, ?, ?) RETURNING id",
                (user_id, author, author_not, query),
            ).fetchone()["id"]
            conn.executemany(
                "INSERT OR IGNORE INTO subscription_chats (subscription_id, chat_id) VALUES (?, ?)",
                [(sub_id, chat) for chat in chats],
            )
    finally:
        conn.close()
    return int(sub_id)


def list_all(db_path: Path, *, user_id: int | None = None) -> list[SubscriptionRow]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT s.id, s.user_id, s.author, s.author_not, s.query, s.active, "
            "group_concat(c.chat_id) AS chats FROM subscriptions s "
            "LEFT JOIN subscription_chats c ON c.subscription_id = s.id "
            "WHERE ? IS NULL OR s.user_id = ? GROUP BY s.id ORDER BY s.id",
            (user_id, user_id),
        ).fetchall()
    finally:
        conn.close()
    return [
        SubscriptionRow(
            id=row["id"],
            user_id=row["user_id"],
            author=row["author"],
            author_not=row["author_not"],
            query=row["query"],
            active=bool(row["active"]),
            chats=tuple(int(c) for c in row["chats"].split(",")) if row["chats"] else (),
        )
        for row in rows
    ]


def deactivate(db_path: Path, subscription_id: int) -> bool:
    conn = connect(db_path)
    try:
        with transaction(conn):
            row = conn.execute(
                "UPDATE subscriptions SET active = 0 WHERE id = ? AND active = 1 RETURNING id",
                (subscription_id,),
            ).fetchone()
    finally:
        conn.close()
    return row is not None
