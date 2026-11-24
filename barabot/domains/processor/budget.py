"""A hard spend limit on the decision model, persisted in the app database.

Every request is recorded in `jev_usage`, so the limit survives restarts and
deploys. Before a request the decider calls `check()`; once the recorded
spend reaches the limit it raises BudgetExhausted and processing stops
asking until the limit is raised.

Spend is input tokens times a configured price; TypeSafe does not bill
output. The price comes from public reports, not from TypeSafe's docs, so
compare with the console now and then.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from barabot.core.db import connect, transaction
from barabot.core.logs import notice

log = logging.getLogger(__name__)

MILESTONE_SHARE = 0.1  # announce every 10% of the limit


class BudgetExhausted(Exception):
    pass


class Budget:
    def __init__(self, db_path: Path, *, limit_usd: float | None, price_per_m_input: float) -> None:
        self._db_path = db_path
        self._limit = limit_usd
        self._price = price_per_m_input
        self._exhausted_reported = False

    @property
    def limit_usd(self) -> float | None:
        return self._limit

    def cost(self, input_tokens: int) -> float:
        return input_tokens * self._price / 1_000_000

    def spent_usd(self) -> float:
        conn = connect(self._db_path)
        try:
            tokens = conn.execute("SELECT coalesce(sum(input_tokens), 0) FROM jev_usage").fetchone()[0]
        finally:
            conn.close()
        return self.cost(tokens)

    def check(self) -> None:
        if self._limit is None:
            return
        spent = self.spent_usd()
        if spent >= self._limit:
            if not self._exhausted_reported:
                log.error(
                    "Jev budget exhausted: $%.4f of $%.2f spent. Matching is paused; "
                    "raise JEV_BUDGET_USD and restart to resume.",
                    spent,
                    self._limit,
                )
                self._exhausted_reported = True
            raise BudgetExhausted(f"${spent:.4f} of ${self._limit:.2f} spent")

    def record(self, *, round: int, questions: int, photos: int, input_tokens: int, output_tokens: int) -> None:
        conn = connect(self._db_path)
        try:
            with transaction(conn):
                before = conn.execute("SELECT coalesce(sum(input_tokens), 0) FROM jev_usage").fetchone()[0]
                conn.execute(
                    "INSERT INTO jev_usage (round, questions, photos, input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?)",
                    (round, questions, photos, input_tokens, output_tokens),
                )
        finally:
            conn.close()
        if self._limit:
            step = self._limit * MILESTONE_SHARE
            old, new = self.cost(before), self.cost(before + input_tokens)
            if int(new // step) > int(old // step):
                notice(log, "Jev spend: $%.3f of $%.2f (%d%%)", new, self._limit, round_pct(new, self._limit))

    async def check_async(self) -> None:
        await asyncio.to_thread(self.check)

    async def record_async(self, **usage: int) -> None:
        await asyncio.to_thread(lambda: self.record(**usage))


def round_pct(part: float, whole: float) -> int:
    return int(100 * part / whole) if whole else 0
