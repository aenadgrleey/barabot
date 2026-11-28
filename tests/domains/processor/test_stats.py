import asyncio

import pytest

from barabot.core.db import connect, migrate
from barabot.core.logs import NOTICE
from barabot.domains.bot import subscriptions
from barabot.domains.budget import Budget
from barabot.domains.processor.listing import message_link
from barabot.domains.processor.stats import StatsReporter

PRICE = 1.0  # $1 per million tokens keeps the arithmetic readable


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "barabot.sqlite"
    migrate(path)
    return path


def pay(budget, tokens, round=1, photos=0, subscription_ids=(1,)):
    budget.record_jev(
        subscription_ids=subscription_ids, round=round, questions=5, photos=photos, input_tokens=tokens, output_tokens=0
    )


def test_digest_counts_only_what_happened_since_the_previous_one(db, caplog):
    budget = Budget(db, jev_limit_usd=1.0, jev_price_per_m_input=PRICE)
    reporter = StatsReporter(db, budget, interval_seconds=3600)
    pay(budget, 1_000)  # before start: not in the first digest
    asyncio.run(reporter.start())
    conn = connect(db)
    conn.execute("INSERT INTO messages (chat_id, msg_id, date) VALUES (-1, 1, 'x'), (-1, 2, 'x')")
    conn.close()
    flat = subscriptions.add(
        db,
        user_id=111,
        author="сдаёт квартиру",
        query="квартира от 2 комнат, до 20 000 Kč в месяц, рядом с метро",
        chats=["@prague_flats", "https://t.me/+AbCdEf"],
    )
    bike = subscriptions.add(db, user_id=222, author="продаёт велосипед", query="горный велосипед", chats=["-1001"])
    idle = subscriptions.add(db, user_id=222, author="продаёт стол", query="стол", chats=["-1001"])
    conn = connect(db)
    conn.execute("UPDATE chats SET username = 'Prague_Flats' WHERE value = 'prague_flats'")
    conn.execute("UPDATE chats SET title = 'Bikes CZ' WHERE value = '-1001'")
    conn.execute(
        "INSERT INTO matches (chat_id, album, ref, version, subscription_id, author_p, choice, confidence, round, matched) "
        f"VALUES (-1, 0, 1, 1, {bike}, 0.9, 'fits', 0.9, 2, 1), (-1, 0, 1, 1, {flat}, 0.1, 'fits', 0.9, 2, 0)"
    )
    conn.close()
    pay(budget, 2_000, subscription_ids=[flat, bike, idle])
    pay(budget, 4_000, round=2, photos=3, subscription_ids=[bike])
    subscriptions.deactivate(db, idle)
    with caplog.at_level(NOTICE):
        first = reporter.report()
        second = reporter.report()
    assert first.splitlines() == [
        (
            "Digest: 2 new message(s), 1 listing(s) checked, 1 match(es); Jev 2 request(s), 1 with photos (3 photos), "
            "6000 tokens (~3000/request, $0.0060); spent $0.007 of $1.00"
        ),
        "user 222: $0.0053, 2 subscription(s)",
        f'  #{bike} $0.0047, 2 request(s), 1 match(es): "продаёт велосипед" / "горный велосипед" in Bikes CZ',
        f'  #{idle} $0.0007, 1 request(s), 0 match(es) (off): "продаёт стол" / "стол" in Bikes CZ',
        "user 111: $0.0007, 1 subscription(s)",
        (
            f'  #{flat} $0.0007, 1 request(s), 0 match(es): "сдаёт квартиру" / "квартира от 2 комнат, до 20 000 Kč в ме…" '
            "in @Prague_Flats, https://t.me/+AbCdEf"
        ),
    ]
    assert second == (
        "Digest: 0 new message(s), 0 listing(s) checked, 0 match(es); Jev 0 request(s), 0 with photos (0 photos), "
        "0 tokens (~0/request, $0.0000); spent $0.007 of $1.00"
    )


def test_message_links():
    assert message_link(-1001234567890, 42) == "https://t.me/c/1234567890/42"
    assert message_link(-4567, 42) == "chat -4567 msg 42"
