import logging

import pytest

from barabot.core.db import connect, migrate
from barabot.core.logs import NOTICE
from barabot.domains.budget import Budget, BudgetExhausted

PRICE = 1.0  # $1 per million tokens keeps the arithmetic readable


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "barabot.sqlite"
    migrate(path)
    return path


def subscribe(db, user_id):
    conn = connect(db)
    try:
        return conn.execute(
            "INSERT INTO subscriptions (user_id, author, query) VALUES (?, 'a', 'q') RETURNING id", (user_id,)
        ).fetchone()["id"]
    finally:
        conn.close()


def pay(budget, tokens, round=1, photos=0, subscription_ids=(1,)):
    budget.record_jev(
        subscription_ids=subscription_ids, round=round, questions=5, photos=photos, input_tokens=tokens, output_tokens=0
    )


def test_budget_blocks_once_the_limit_is_reached_and_survives_restarts(db, caplog):
    budget = Budget(db, jev_limit_usd=1.0, jev_price_per_m_input=PRICE)
    budget.check_jev()
    pay(budget, 600_000)
    budget.check_jev()
    pay(budget, 400_000)
    with caplog.at_level(logging.ERROR), pytest.raises(BudgetExhausted):
        budget.check_jev()
    assert "budget exhausted" in caplog.text
    # a new process sees the same spend
    with pytest.raises(BudgetExhausted):
        Budget(db, jev_limit_usd=1.0, jev_price_per_m_input=PRICE).check_jev()
    Budget(db, jev_limit_usd=2.0, jev_price_per_m_input=PRICE).check_jev()  # raising the limit resumes


def test_budget_announces_every_tenth_of_the_limit(db, caplog):
    budget = Budget(db, jev_limit_usd=1.0, jev_price_per_m_input=PRICE)
    with caplog.at_level(NOTICE):
        pay(budget, 50_000)  # 5%: nothing
        pay(budget, 60_000)  # 11%: crosses 10%
        pay(budget, 250_000)  # 36%: crosses 20% and 30%, one line
    lines = [r.getMessage() for r in caplog.records if r.levelno == NOTICE]
    assert lines == ["Jev spend: $0.110 of $1.00 (11%)", "Jev spend: $0.360 of $1.00 (36%)"]


def test_no_limit_never_blocks(db):
    budget = Budget(db, jev_limit_usd=None, jev_price_per_m_input=PRICE)
    pay(budget, 10_000_000)
    budget.check_jev()


def test_a_request_is_split_equally_between_the_subscriptions_it_checked(db):
    alice_flat, alice_bike, bob_flat = subscribe(db, 111), subscribe(db, 111), subscribe(db, 222)
    budget = Budget(db, jev_limit_usd=None, jev_price_per_m_input=PRICE)
    pay(budget, 1_000, subscription_ids=[alice_flat, alice_bike, bob_flat])  # $0.001 = 1_000_000 nano-dollars
    pay(budget, 2_000, round=2, subscription_ids=[bob_flat])  # the photo round: only bob was unclear
    users = {row.user_id: row for row in budget.by_user()}
    assert users[111].nano_usd == 666_667  # two shares of three: 333_334 + 333_333
    assert users[222].nano_usd == 333_333 + 2_000_000
    assert (users[111].requests, users[222].requests) == (1, 2)
    assert budget.jev_spent_usd() == pytest.approx(0.003)  # the shares add up to the requests exactly


def test_a_new_price_applies_to_new_requests_only(db):
    pay(Budget(db, jev_limit_usd=None, jev_price_per_m_input=1.0), 1_000_000)
    budget = Budget(db, jev_limit_usd=None, jev_price_per_m_input=3.0)
    assert budget.jev_spent_usd() == pytest.approx(1.0)
    pay(budget, 1_000_000)
    assert budget.jev_spent_usd() == pytest.approx(4.0)


def test_requests_from_before_the_ledger_count_toward_the_limit_once(db):
    conn = connect(db)
    conn.execute(
        "INSERT INTO jev_usage (at, round, questions, photos, input_tokens, output_tokens) "
        "VALUES ('2026-11-01 10:00:00', 1, 3, 0, 700000, 0), ('2026-11-02 10:00:00', 2, 1, 2, 300000, 0)"
    )
    conn.close()
    budget = Budget(db, jev_limit_usd=1.0, jev_price_per_m_input=PRICE)
    assert budget.adopt_unledgered() == 2
    assert budget.adopt_unledgered() == 0
    with pytest.raises(BudgetExhausted):
        budget.check_jev()
    assert [(row.user_id, row.nano_usd) for row in budget.by_user()] == [(None, 1_000_000_000)]
    assert budget.by_user(since="2026-11-02 00:00:00")[0].nano_usd == 300_000_000
