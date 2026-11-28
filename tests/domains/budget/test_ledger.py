import pytest

from barabot.core.db import connect, migrate, transaction
from barabot.domains.budget import JEV, Budget, ledger


def test_split_adds_up_and_gives_the_remainder_to_the_first_shares():
    assert ledger.split(10, 3) == [4, 3, 3]
    assert ledger.split(2, 4) == [1, 1, 0, 0]
    assert ledger.split(0, 2) == [0, 0]
    with pytest.raises(ValueError):
        ledger.split(5, 0)


def test_a_share_of_a_missing_subscription_is_charged_to_no_one(tmp_path):
    db = tmp_path / "barabot.sqlite"
    migrate(db)
    conn = connect(db)
    try:
        sub = conn.execute("INSERT INTO subscriptions (user_id, author, query) VALUES (7, 'a', 'q') RETURNING id")
        sub_id = sub.fetchone()["id"]
        with transaction(conn):
            ledger.charge(conn, source=JEV, request_id=1, nano_usd=101, subscription_ids=[sub_id, 999])
        assert ledger.total(conn) == 101
        assert ledger.total(conn, "agent") == 0
    finally:
        conn.close()
    budget = Budget(db, jev_limit_usd=None, jev_price_per_m_input=1.0)
    assert [(row.user_id, row.nano_usd) for row in budget.by_user()] == [(7, 51), (None, 50)]
    assert [day.nano_usd for day in budget.by_day(days=1)] == [101]
