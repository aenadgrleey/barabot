"""`Budget`: the one entry point to what barabot spends.

Every paid request is logged and charged here, the Jev limit is checked
here, and every total comes from here. `__main__` builds one instance and
injects it into each part that pays or reports: the Jev decider, the
digest, the `spend` CLI. Nothing else reads or writes `jev_usage` or
`spend`. How the ledger stores and splits a charge is in `ledger.py`.

Jev spend is input tokens times the configured price at the time of the
request; TypeSafe does not bill output. A new price applies to new
requests only. The price comes from public reports, not from TypeSafe's
docs, so compare with the console now and then.

The Jev limit counts the ledger's Jev spend, so it survives restarts and
deploys. Before a request the decider calls `check_jev()`; once the spend
reaches the limit it raises BudgetExhausted and processing stops asking
until the limit is raised.

Blocking methods open a short-lived connection; call them through the
`*_async` variants or `asyncio.to_thread` from async code.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from barabot.core.db import connect, transaction
from barabot.core.logs import notice
from barabot.domains.budget import ledger
from barabot.domains.budget.ledger import JEV, NANO_PER_USD

log = logging.getLogger(__name__)

MILESTONE_SHARE = 0.1  # announce every 10% of the Jev limit


class BudgetExhausted(Exception):
    pass


class Mark(NamedTuple):
    """Where the logs end at one moment; two marks bound a period.

    Row IDs, not times: datetime('now') has one-second resolution.
    """

    jev_request_id: int
    spend_id: int


START = Mark(0, 0)  # before anything was spent


@dataclass(frozen=True)
class JevRequests:
    """Jev requests in a period."""

    requests: int
    photo_requests: int  # round 2: text and photos
    photos: int
    input_tokens: int
    nano_usd: int


@dataclass(frozen=True)
class Charge:
    """What a period charged to one subscription."""

    user_id: int | None  # None: spend from before the ledger, or a subscription that is gone
    subscription_id: int | None
    nano_usd: int
    requests: int  # paid requests that carried a share for this subscription


@dataclass(frozen=True)
class UserSpend:
    user_id: int | None  # None: spend from before the ledger
    nano_usd: int
    requests: int  # distinct paid requests that carried a share for this user


@dataclass(frozen=True)
class DaySpend:
    day: str  # UTC date, YYYY-MM-DD
    nano_usd: int


class Budget:
    def __init__(self, db_path: Path, *, jev_limit_usd: float | None, jev_price_per_m_input: float) -> None:
        self._db_path = db_path
        self._jev_limit = jev_limit_usd
        self._jev_price = jev_price_per_m_input
        self._exhausted_reported = False

    @property
    def jev_limit_usd(self) -> float | None:
        return self._jev_limit

    # Jev: price, limit, recording

    def jev_cost_nano(self, input_tokens: int) -> int:
        """Price of a Jev request at the current price, in nano-dollars."""
        return round(input_tokens * self._jev_price * NANO_PER_USD / 1_000_000)

    def jev_spent_usd(self) -> float:
        return ledger.to_usd(self.total_nano(JEV))

    def check_jev(self) -> None:
        """Raise BudgetExhausted once Jev spend has reached the limit."""
        if self._jev_limit is None:
            return
        spent = self.jev_spent_usd()
        if spent >= self._jev_limit:
            if not self._exhausted_reported:
                log.error(
                    "Jev budget exhausted: $%.4f of $%.2f spent. Matching is paused; "
                    "raise JEV_BUDGET_USD and restart to resume.",
                    spent,
                    self._jev_limit,
                )
                self._exhausted_reported = True
            raise BudgetExhausted(f"${spent:.4f} of ${self._jev_limit:.2f} spent")

    def record_jev(
        self,
        *,
        subscription_ids: Sequence[int],
        round: int,
        questions: int,
        photos: int,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Log one Jev request and charge it to the subscriptions it checked, in one transaction."""
        cost = self.jev_cost_nano(input_tokens)
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                before = ledger.total(conn, JEV)
                request_id = conn.execute(
                    "INSERT INTO jev_usage (round, questions, photos, input_tokens, output_tokens) "
                    "VALUES (?, ?, ?, ?, ?) RETURNING id",
                    (round, questions, photos, input_tokens, output_tokens),
                ).fetchone()["id"]
                ledger.charge(conn, source=JEV, request_id=request_id, nano_usd=cost, subscription_ids=subscription_ids)
        finally:
            conn.close()
        if self._jev_limit:
            step = self._jev_limit * MILESTONE_SHARE
            old, new = ledger.to_usd(before), ledger.to_usd(before + cost)
            if int(new // step) > int(old // step):
                notice(log, "Jev spend: $%.3f of $%.2f (%d%%)", new, self._jev_limit, round_pct(new, self._jev_limit))

    def adopt_unledgered(self) -> int:
        """Charge Jev requests logged before the ledger existed to no one, at the current price.

        Without them the limit would restart from zero after the upgrade.
        Every later request is charged when it is logged, so this finds
        nothing after the first run. Returns how many requests it adopted.
        """
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                rows = conn.execute(
                    "SELECT id, at, input_tokens FROM jev_usage u WHERE NOT EXISTS "
                    "(SELECT 1 FROM spend s WHERE s.source = ? AND s.request_id = u.id)",
                    (JEV,),
                ).fetchall()
                conn.executemany(
                    "INSERT INTO spend (at, source, request_id, nano_usd) VALUES (?, ?, ?, ?)",
                    [(row["at"], JEV, row["id"], self.jev_cost_nano(row["input_tokens"])) for row in rows],
                )
        finally:
            conn.close()
        return len(rows)

    async def check_jev_async(self) -> None:
        await asyncio.to_thread(self.check_jev)

    async def record_jev_async(self, *, subscription_ids: Sequence[int], **usage: int) -> None:
        await asyncio.to_thread(lambda: self.record_jev(subscription_ids=subscription_ids, **usage))

    # Projections of the ledger

    def total_nano(self, source: str | None = None) -> int:
        """All spend in nano-dollars, or the spend of one source."""
        conn = connect(self._db_path)
        try:
            return ledger.total(conn, source)
        finally:
            conn.close()

    def by_user(self, *, since: str | None = None) -> list[UserSpend]:
        """Spend per user since a SQLite datetime (all time without it), largest first."""
        conn = connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT user_id, sum(nano_usd) AS nano, count(DISTINCT source || ':' || request_id) AS requests "
                "FROM spend WHERE ? IS NULL OR at >= ? GROUP BY user_id ORDER BY nano DESC, user_id",
                (since, since),
            ).fetchall()
        finally:
            conn.close()
        return [UserSpend(row["user_id"], row["nano"], row["requests"]) for row in rows]

    def by_day(self, *, days: int) -> list[DaySpend]:
        """Spend per UTC day over the last `days` days, oldest first; days without spend are left out."""
        conn = connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT date(at) AS day, sum(nano_usd) AS nano FROM spend "
                "WHERE at >= datetime('now', ?) GROUP BY day ORDER BY day",
                (f"-{days} days",),
            ).fetchall()
        finally:
            conn.close()
        return [DaySpend(row["day"], row["nano"]) for row in rows]

    # Periods, for the digest

    def mark(self) -> Mark:
        conn = connect(self._db_path)
        try:
            return Mark(
                conn.execute("SELECT coalesce(max(id), 0) FROM jev_usage").fetchone()[0],
                conn.execute("SELECT coalesce(max(id), 0) FROM spend").fetchone()[0],
            )
        finally:
            conn.close()

    def jev_requests(self, after: Mark, upto: Mark) -> JevRequests:
        """Jev requests logged after `after` and up to `upto`, with what they cost."""
        conn = connect(self._db_path)
        try:
            usage = conn.execute(
                "SELECT count(*) AS requests, coalesce(sum(round = 2), 0) AS photo_requests, "
                "coalesce(sum(photos), 0) AS photos, coalesce(sum(input_tokens), 0) AS tokens "
                "FROM jev_usage WHERE id > ? AND id <= ?",
                (after.jev_request_id, upto.jev_request_id),
            ).fetchone()
            nano = conn.execute(
                "SELECT coalesce(sum(nano_usd), 0) FROM spend WHERE source = ? AND request_id > ? AND request_id <= ?",
                (JEV, after.jev_request_id, upto.jev_request_id),
            ).fetchone()[0]
        finally:
            conn.close()
        return JevRequests(usage["requests"], usage["photo_requests"], usage["photos"], usage["tokens"], nano)

    def charges(self, after: Mark, upto: Mark) -> list[Charge]:
        """What the ledger charged after `after` and up to `upto`, per user and subscription."""
        conn = connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT user_id, subscription_id, sum(nano_usd) AS nano, "
                "count(DISTINCT source || ':' || request_id) AS requests FROM spend "
                "WHERE id > ? AND id <= ? GROUP BY user_id, subscription_id",
                (after.spend_id, upto.spend_id),
            ).fetchall()
        finally:
            conn.close()
        return [Charge(row["user_id"], row["subscription_id"], row["nano"], row["requests"]) for row in rows]


def round_pct(part: float, whole: float) -> int:
    return int(100 * part / whole) if whole else 0
