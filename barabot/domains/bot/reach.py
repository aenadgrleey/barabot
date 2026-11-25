"""Whether the bot can write to a user, as the last attempt showed.

Telegram does not tell a bot who blocked it or never pressed Start; only a
failed send does. Notifier marks a user on Undeliverable and clears the
mark on a delivered message; /start and a delivered MCP welcome clear it
too. The MCP tools refuse to add or change subscriptions while it is set,
since nothing they set up could reach the user.

Blocking calls; run them through `asyncio.to_thread` from async code.
"""

from __future__ import annotations

from pathlib import Path

from barabot.core.db import connect, transaction


def mark_unreachable(db_path: Path, user_id: int, reason: str) -> None:
    conn = connect(db_path)
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO bot_unreachable (user_id, reason) VALUES (?, ?) "
                "ON CONFLICT (user_id) DO UPDATE SET reason = excluded.reason",
                (user_id, reason),
            )
    finally:
        conn.close()


def mark_reachable(db_path: Path, user_id: int) -> None:
    conn = connect(db_path)
    try:
        with transaction(conn):
            conn.execute("DELETE FROM bot_unreachable WHERE user_id = ?", (user_id,))
    finally:
        conn.close()


def unreachable_reason(db_path: Path, user_id: int) -> str | None:
    """Why the last message to the user failed, or None when the bot can write to them."""
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT reason FROM bot_unreachable WHERE user_id = ?", (user_id,)).fetchone()
        return row["reason"] if row else None
    finally:
        conn.close()
