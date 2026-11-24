import asyncio
import logging

import pytest

from barabot.core.db import connect, migrate
from barabot.core.logs import NOTICE
from barabot.domains.processor.budget import Budget, BudgetExhausted
from barabot.domains.processor.listing import message_link
from barabot.domains.processor.stats import StatsReporter

PRICE = 1.0  # $1 per million tokens keeps the arithmetic readable


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "barabot.sqlite"
    migrate(path)
    return path


def spend(budget, tokens, round=1, photos=0):
    budget.record(round=round, questions=5, photos=photos, input_tokens=tokens, output_tokens=0)


def test_budget_blocks_once_the_limit_is_reached_and_survives_restarts(db, caplog):
    budget = Budget(db, limit_usd=1.0, price_per_m_input=PRICE)
    budget.check()
    spend(budget, 600_000)
    budget.check()
    spend(budget, 400_000)
    with caplog.at_level(logging.ERROR), pytest.raises(BudgetExhausted):
        budget.check()
    assert "budget exhausted" in caplog.text
    # a new process sees the same spend
    with pytest.raises(BudgetExhausted):
        Budget(db, limit_usd=1.0, price_per_m_input=PRICE).check()
    Budget(db, limit_usd=2.0, price_per_m_input=PRICE).check()  # raising the limit resumes


def test_budget_announces_every_tenth_of_the_limit(db, caplog):
    budget = Budget(db, limit_usd=1.0, price_per_m_input=PRICE)
    with caplog.at_level(NOTICE):
        spend(budget, 50_000)  # 5%: nothing
        spend(budget, 60_000)  # 11%: crosses 10%
        spend(budget, 250_000)  # 36%: crosses 20% and 30%, one line
    lines = [r.getMessage() for r in caplog.records if r.levelno == NOTICE]
    assert lines == ["Jev spend: $0.110 of $1.00 (11%)", "Jev spend: $0.360 of $1.00 (36%)"]


def test_no_limit_never_blocks(db):
    budget = Budget(db, limit_usd=None, price_per_m_input=PRICE)
    spend(budget, 10_000_000)
    budget.check()


def test_digest_counts_only_what_happened_since_the_previous_one(db, caplog):
    budget = Budget(db, limit_usd=1.0, price_per_m_input=PRICE)
    reporter = StatsReporter(db, budget, interval_seconds=3600)
    spend(budget, 1_000)  # before start: not in the first digest
    asyncio.run(reporter.start())
    conn = connect(db)
    conn.execute("INSERT INTO messages (chat_id, msg_id, date) VALUES (-1, 1, 'x'), (-1, 2, 'x')")
    conn.execute(
        "INSERT INTO matches (chat_id, album, ref, version, subscription_id, author_p, choice, confidence, round, matched) "
        "VALUES (-1, 0, 1, 1, 1, 0.9, 'fits', 0.9, 2, 1), (-1, 0, 1, 1, 2, 0.1, 'fits', 0.9, 2, 0)"
    )
    conn.close()
    spend(budget, 2_000)
    spend(budget, 4_000, round=2, photos=3)
    with caplog.at_level(NOTICE):
        first = reporter.report()
        second = reporter.report()
    assert first.startswith("Digest: 2 new message(s), 1 listing(s) checked, 1 match(es); Jev 2 request(s), 1 with photos (3 photos), 6000 tokens")
    assert second.startswith("Digest: 0 new message(s), 0 listing(s) checked, 0 match(es); Jev 0 request(s)")
    assert "spent $0.007 of $1.00" in second
    assert first.endswith("matches by subscription: #1×1")
    assert "matches by subscription" not in second


def test_message_links():
    assert message_link(-1001234567890, 42) == "https://t.me/c/1234567890/42"
    assert message_link(-4567, 42) == "chat -4567 msg 42"
