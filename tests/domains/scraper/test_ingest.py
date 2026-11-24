"""MessageIngest without DBOS: what it stores and which revisions it schedules."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from barabot.core.db import ListingKey, connect, migrate
from barabot.domains.scraper.ingest import MessageIngest
from barabot.domains.scraper.model.event import DeletedMessages, SeenMessage

CHANNEL = -1001234567890
GROUP = -4567
ALBUM = 13_000_000_000_000_001


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[ListingKey, int]] = []

    async def __call__(self, key: ListingKey, rev: int) -> None:
        self.calls.append((key, rev))


@pytest.fixture
def path(tmp_path: Path) -> Path:
    db = tmp_path / "barabot.sqlite"
    migrate(db)
    return db


def ingest_all(path: Path, *events) -> list[tuple[ListingKey, int]]:
    scheduled = Recorder()
    ingest = MessageIngest(path, scheduled)

    async def main():
        for event in events:
            if isinstance(event, SeenMessage):
                await ingest.save_message(event)
            else:
                await ingest.mark_deleted(event)

    asyncio.run(main())
    return scheduled.calls


def seen(chat_id, msg_id, grouped_id=None, text="") -> SeenMessage:
    return SeenMessage(chat_id, msg_id, grouped_id, 42, datetime(2026, 11, 23, tzinfo=UTC), text)


def test_album_messages_share_a_listing_and_bump_its_rev(path):
    key = ListingKey(CHANNEL, True, ALBUM)
    calls = ingest_all(path, *(seen(CHANNEL, i, ALBUM) for i in (10, 11, 12)))
    assert calls == [(key, 1), (key, 2), (key, 3)]


def test_edit_updates_text_and_bumps_rev(path):
    calls = ingest_all(path, seen(CHANNEL, 10, text="old"), seen(CHANNEL, 10, text="new"))
    assert calls[-1] == (ListingKey(CHANNEL, False, 10), 2)
    conn = connect(path)
    assert conn.execute("SELECT text FROM messages").fetchone()["text"] == "new"
    conn.close()


def test_deleting_two_photos_of_one_album_bumps_once(path):
    calls = ingest_all(
        path,
        *(seen(CHANNEL, i, ALBUM) for i in (10, 11, 12)),
        DeletedMessages(CHANNEL, [10, 11]),
    )
    assert calls[3:] == [(ListingKey(CHANNEL, True, ALBUM), 4)]


def test_repeated_delete_is_a_no_op(path):
    calls = ingest_all(path, seen(CHANNEL, 10), DeletedMessages(CHANNEL, [10]), DeletedMessages(CHANNEL, [10]))
    assert len(calls) == 2


def test_delete_without_chat_id_skips_channels(path):
    calls = ingest_all(path, seen(CHANNEL, 10), seen(GROUP, 10), DeletedMessages(None, [10]))
    assert calls[2:] == [(ListingKey(GROUP, False, 10), 2)]


def test_delete_of_unknown_message_schedules_nothing(path):
    assert ingest_all(path, DeletedMessages(CHANNEL, [99])) == []
