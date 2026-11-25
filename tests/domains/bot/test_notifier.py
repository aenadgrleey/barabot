"""Notifier: what a user should see, message text, and the full path on a real DBOS.

The end-to-end tests run ListingProcessor with a Notifier and a fake bot,
so they see which messages a user would get as a listing changes.
"""

import asyncio
from datetime import UTC, datetime

import pytest
from dbos import DBOS

from barabot.core import durable
from barabot.core.db import ListingKey, connect, migrate
from barabot.domains.bot import reach, subscriptions
from barabot.domains.bot.notifier import (
    MATCHED,
    REMOVED,
    UNMATCHED,
    Notifier,
    Shown,
    Undeliverable,
    render,
    target,
    workflow_id,
)
from barabot.domains.processor.listing import ListingProcessor
from barabot.domains.processor.matching import Decision
from barabot.domains.scraper.ingest import MessageIngest
from barabot.domains.scraper.model.event import DeletedMessages, SeenMessage

CHAT = -1001234567890
KEY = ListingKey(CHAT, False, 10)
USER = 5


# --- what a user should see ---------------------------------------------


def shown(version=1, state=MATCHED):
    return Shown(version, state, 100)


@pytest.mark.parametrize(
    ("removed", "matched", "seen", "expected"),
    [
        (False, True, None, MATCHED),
        (False, True, shown(1), None),
        (False, True, shown(0), MATCHED),
        (False, True, shown(1, UNMATCHED), MATCHED),
        (False, True, shown(1, REMOVED), MATCHED),
        (False, False, None, None),
        (False, False, shown(1), UNMATCHED),
        (False, False, shown(0, UNMATCHED), None),
        (True, False, None, None),
        (True, False, shown(1), REMOVED),
        (True, False, shown(1, UNMATCHED), REMOVED),
        (True, False, shown(1, REMOVED), None),
    ],
)
def test_target(removed, matched, seen, expected):
    assert target(removed, matched, 1, seen) == expected


def test_render():
    assert render(MATCHED, "https://t.me/x/1", ["квартира", "у метро"]) == (
        'Matches "квартира", "у метро"\nhttps://t.me/x/1'
    )
    assert render(UNMATCHED, "https://t.me/x/1", []) == "Edited, no longer matches\nhttps://t.me/x/1"
    assert render(REMOVED, None, []) == "The listing was deleted"


# --- end to end --------------------------------------------------------------


class FakeBot:
    def __init__(self, undeliverable=False):
        self.undeliverable = undeliverable
        self.sent: list[tuple[int, int, str]] = []
        self.edits: list[tuple[int, int, str]] = []

    async def send(self, user_id, text):
        if self.undeliverable:
            raise Undeliverable("blocked")
        self.sent.append((user_id, len(self.sent) + 1, text))
        return len(self.sent)

    async def edit(self, user_id, message_id, text):
        self.edits.append((user_id, message_id, text))


class KeywordDecider:
    """Answers "fits" when the subscription's query is a word of the text."""

    async def decide(self, text, subs, photos=()):
        return [Decision(s.id, 0.95, "fits" if s.query in text else "does_not_fit", 0.9, 1) for s in subs]


def msg(text, msg_id=10):
    return SeenMessage(CHAT, msg_id, None, 42, datetime(2026, 11, 24, tzinfo=UTC), text, [])


def run(tmp_path, scenario, bot=None):
    db = tmp_path / "barabot.sqlite"
    migrate(db)
    subscriptions.add(db, user_id=USER, author="сдаёт жильё", query="балкон", chats=[CHAT])
    bot = bot or FakeBot()
    notifier = Notifier(db, bot, name=tmp_path.name)
    processor = ListingProcessor(
        db, tmp_path, KeywordDecider(), notifier=notifier, debounce_seconds=0.2, name=tmp_path.name
    )

    async def main():
        durable.launch(tmp_path / "dbos.sqlite")
        try:
            await processor.open()
            await notifier.open()
            await scenario(db, MessageIngest(db, processor.schedule))
        finally:
            durable.shutdown()

    asyncio.run(main())
    return bot


async def processed(rev):
    return await (await DBOS.retrieve_workflow_async(KEY.workflow_id(rev))).get_result()


async def notified(rev):
    return await (await DBOS.retrieve_workflow_async(workflow_id(KEY, rev))).get_result()


def delivery(db):
    conn = connect(db)
    try:
        row = conn.execute("SELECT version, state, bot_message_id FROM deliveries").fetchone()
        return tuple(row) if row else None
    finally:
        conn.close()


def test_a_match_is_sent_once_and_edited_as_the_listing_changes(tmp_path):
    async def scenario(db, ingest):
        await ingest.save_message(msg("Сдаю квартиру, есть балкон"))
        assert await processed(1) == "version 1: 1 match(es)"
        assert await notified(1) == "5 sent"

        await ingest.save_message(msg("Сдаю квартиру, есть балкон, 900 EUR"))
        assert await processed(2) == "version 2: 1 match(es)"
        assert await notified(2) == "5 edited"

        await ingest.save_message(msg("Сдаю квартиру, 900 EUR"))
        assert await processed(3) == "version 3: 0 match(es)"
        assert await notified(3) == "5 edited"

        await ingest.mark_deleted(DeletedMessages(CHAT, [10]))
        assert await processed(4) == "version 4: 0 match(es)"
        assert await notified(4) == "5 edited"
        assert delivery(db) == (4, "removed", 1)

    bot = run(tmp_path, scenario)
    assert [(u, m) for u, m, _ in bot.sent] == [(USER, 1)]
    assert bot.sent[0][2] == ('Matches "балкон"\nhttps://t.me/c/1234567890/10')
    assert [text.split("\n")[0] for _, _, text in bot.edits] == [
        'Matches "балкон"',
        "Edited, no longer matches",
        "The listing was deleted",
    ]
    assert all(m == 1 for _, m, _ in bot.edits)


def test_a_public_chat_links_by_username(tmp_path):
    async def scenario(db, ingest):
        conn = connect(db)
        conn.execute("INSERT INTO chats (kind, value, chat_id) VALUES ('username', 'prague_flats', ?)", (CHAT,))
        conn.close()
        await ingest.save_message(msg("балкон"))
        assert await processed(1) == "version 1: 1 match(es)"
        assert await notified(1) == "5 sent"

    bot = run(tmp_path, scenario)
    assert bot.sent[0][2].endswith("https://t.me/prague_flats/10")


def test_an_unchanged_listing_is_routed_again(tmp_path):
    """A run cancelled after deciding loses its notify; the next run, even unchanged, sends it.

    The lost notify is simulated by dropping the delivery it recorded.
    """

    async def scenario(db, ingest):
        await ingest.save_message(msg("балкон"))
        assert await processed(1) == "version 1: 1 match(es)"
        assert await notified(1) == "5 sent"
        conn = connect(db)
        conn.execute("DELETE FROM deliveries")
        conn.close()

        await ingest.save_message(msg("балкон"))
        assert await processed(2) == "unchanged"
        assert await notified(2) == "5 sent"
        assert (delivery(db) or ())[:2] == (1, "matched")

    run(tmp_path, scenario)


def test_a_delivered_message_clears_the_unreachable_mark(tmp_path):
    async def scenario(db, ingest):
        reach.mark_unreachable(db, USER, "blocked")
        await ingest.save_message(msg("балкон"))
        assert await processed(1) == "version 1: 1 match(es)"
        await notified(1)
        assert reach.unreachable_reason(db, USER) is None

    run(tmp_path, scenario)


def test_nobody_to_tell_sends_nothing(tmp_path):
    async def scenario(db, ingest):
        await ingest.save_message(msg("без нужного слова"))
        assert await processed(1) == "version 1: 0 match(es)"
        await ingest.save_message(msg("без нужного слова"))
        assert await processed(2) == "unchanged"
        assert await notified(1) == await notified(2) == "nobody"

    bot = run(tmp_path, scenario)
    assert bot.sent == bot.edits == []


def test_an_unreachable_user_is_not_retried_or_recorded(tmp_path):
    async def scenario(db, ingest):
        await ingest.save_message(msg("балкон"))
        assert await processed(1) == "version 1: 1 match(es)"
        assert await notified(1) == "5 undeliverable"
        assert delivery(db) is None
        assert reach.unreachable_reason(db, USER) == "blocked"

    run(tmp_path, scenario, FakeBot(undeliverable=True))


def test_matches_record_the_subscription_revision(tmp_path):
    async def scenario(db, ingest):
        [sub] = subscriptions.list_all(db)
        subscriptions.update(db, sub.id, user_id=USER, author="сдаёт квартиру")
        await ingest.save_message(msg("балкон"))
        assert await processed(1) == "version 1: 1 match(es)"
        conn = connect(db)
        assert conn.execute("SELECT subscription_revision FROM matches").fetchone()[0] == 2
        conn.close()

    run(tmp_path, scenario)


def test_one_notify_tells_every_user_their_own_state(tmp_path):
    async def scenario(db, ingest):
        subscriptions.add(db, user_id=7, author="сдаёт жильё", query="метро", chats=[CHAT])
        await ingest.save_message(msg("балкон и метро"))
        assert await processed(1) == "version 1: 2 match(es)"
        assert await notified(1) == "5 sent, 7 sent"
        await ingest.save_message(msg("только балкон"))
        assert await processed(2) == "version 2: 1 match(es)"
        assert await notified(2) == "5 edited, 7 edited"

    bot = run(tmp_path, scenario)
    assert [u for u, _, _ in bot.sent] == [5, 7]
    assert [(u, text.split("\n")[0]) for u, _, text in bot.edits] == [
        (5, 'Matches "балкон"'),
        (7, "Edited, no longer matches"),
    ]
