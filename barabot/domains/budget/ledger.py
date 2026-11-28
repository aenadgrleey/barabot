"""The `spend` ledger at the SQL level. Only `Budget` calls this module.

A paid request writes its own log row (for Jev, `jev_usage`) and, in the
same transaction, one `spend` row per subscription it served, with that
subscription's user. Every total is a sum over this one table: all spend,
one source, one user, one day. The ledger is append-only.

Amounts are integer nano-dollars (1e-9 USD), priced once when the row is
written. A later price change does not reprice history. A Jev request
with text only costs a few thousand nano-dollars, so integer shares lose
nothing that matters.

A request shared by N subscriptions is split into N equal shares. The
remainder of the division goes one nano-dollar each to the first shares,
so the shares add up to the request exactly. A user with two
subscriptions in the request pays two shares.

Rows with `user_id` NULL hold spend recorded before the ledger existed.

Sources:
    jev    a TypeSafe Jev request; request_id is jev_usage.id
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

NANO_PER_USD = 1_000_000_000

JEV = "jev"


def to_usd(nano: int) -> float:
    return nano / NANO_PER_USD


def split(total: int, parts: int) -> list[int]:
    """`total` in `parts` integer shares that add up to it exactly; the first shares take the remainder."""
    if parts < 1:
        raise ValueError("a charge needs at least one share")
    share, remainder = divmod(total, parts)
    return [share + 1 if i < remainder else share for i in range(parts)]


def charge(
    conn: sqlite3.Connection, *, source: str, request_id: int, nano_usd: int, subscription_ids: Sequence[int]
) -> None:
    """Split one request between the subscriptions it served. Call inside the caller's transaction.

    Each share is charged to the subscription's user. A subscription that
    is gone is charged to no one, so the total still adds up.
    """
    marks = ",".join("?" * len(subscription_ids))
    owners = dict(
        conn.execute(f"SELECT id, user_id FROM subscriptions WHERE id IN ({marks})", list(subscription_ids)).fetchall()
    )
    conn.executemany(
        "INSERT INTO spend (source, request_id, user_id, subscription_id, nano_usd) VALUES (?, ?, ?, ?, ?)",
        [
            (source, request_id, owners.get(sub_id), sub_id, share)
            for sub_id, share in zip(subscription_ids, split(nano_usd, len(subscription_ids)), strict=True)
        ],
    )


def total(conn: sqlite3.Connection, source: str | None = None) -> int:
    """All spend in nano-dollars, or the spend of one source."""
    return conn.execute(
        "SELECT coalesce(sum(nano_usd), 0) FROM spend WHERE ? IS NULL OR source = ?", (source, source)
    ).fetchone()[0]
