"""ListingProcessor on a real DBOS with SQLite in tmp_path.

The debounce is shortened to keep the suite fast; queue polling is 0.2 s.
"""
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dbos import DBOS, SetWorkflowID

from barabot.core import durable
from barabot.core.db import ListingKey, connect, migrate
from barabot.domains.processor.listing import ListingProcessor
from barabot.domains.scraper.ingest import MessageIngest
from barabot.domains.scraper.model.event import DeletedMessages, SeenMessage

CHAT = -1001234567890
ALBUM = 13_000_000_000_000_001
KEY = ListingKey(CHAT, True, ALBUM)


def run(tmp_path: Path, scenario):
    db = tmp_path / "barabot.sqlite"
    migrate(db)

    # One instance per config_name per process, so every test gets its own.
    processor = ListingProcessor(db, debounce_seconds=0.3, name=tmp_path.name)

    async def main():
        durable.launch(tmp_path / "dbos.sqlite")
        try:
            await processor.open()
            return await scenario(db, processor, MessageIngest(db, processor.schedule))
        finally:
            durable.shutdown()

    return asyncio.run(main())


def photo(msg_id, text="", photo_id=None) -> SeenMessage:
    media = [{"type": "photo", "id": photo_id or msg_id}]
    return SeenMessage(CHAT, msg_id, ALBUM, 42, datetime(2026, 11, 23, tzinfo=UTC), text, media)


async def result(rev: int):
    handle = await DBOS.retrieve_workflow_async(KEY.workflow_id(rev))
    return await handle.get_result()


async def status(rev: int) -> str:
    return (await DBOS.get_workflow_status_async(KEY.workflow_id(rev))).status


def row(db: Path):
    conn = connect(db)
    try:
        return conn.execute("SELECT * FROM listings WHERE chat_id = ? AND album = ? AND ref = ?", KEY).fetchone()
    finally:
        conn.close()


def test_album_burst_runs_only_the_last_revision(tmp_path):
    async def scenario(db, processor, ingest):
        for msg in (photo(10, "2 rooms, 900 EUR"), photo(11), photo(12)):
            await ingest.save_message(msg)
        assert await result(3) == "version 1"
        return [await status(rev) for rev in (1, 2)]

    assert run(tmp_path, scenario) == ["CANCELLED", "CANCELLED"]
    listing_row = row(tmp_path / "barabot.sqlite")
    assert listing_row["text"] == "2 rooms, 900 EUR"
    assert [m["id"] for m in json.loads(listing_row["media"])] == [10, 11, 12]
    assert (listing_row["version"], listing_row["removed"]) == (1, 0)


def test_retouch_without_change_keeps_the_version(tmp_path):
    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "caption"))
        assert await result(1) == "version 1"
        await ingest.save_message(photo(10, "caption"))
        return await result(2)

    assert run(tmp_path, scenario) == "unchanged"
    assert row(tmp_path / "barabot.sqlite")["version"] == 1


def test_edit_and_delete_produce_new_versions(tmp_path):
    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "900 EUR"))
        await ingest.save_message(photo(11))
        assert await result(2) == "version 1"
        await ingest.save_message(photo(10, "850 EUR"))
        assert await result(3) == "version 2"
        assert row(db)["text"] == "850 EUR"
        await ingest.mark_deleted(DeletedMessages(CHAT, [10, 11]))
        return await result(4)

    assert run(tmp_path, scenario) == "version 3"
    listing_row = row(tmp_path / "barabot.sqlite")
    assert (listing_row["removed"], listing_row["text"], listing_row["media"]) == (1, "", "[]")


def test_concurrent_events_leave_one_live_run(tmp_path):
    """A is slow to enqueue rev 1; B bumps to 2 meanwhile. Without the ingest
    lock B cancels rev 1 before it exists, then A enqueues it and it runs."""

    async def scenario(db, processor, _):
        async def slow_first(key, rev):
            if rev == 1:
                await asyncio.sleep(0.1)
            await processor.schedule(key, rev)

        ingest = MessageIngest(db, schedule=slow_first)
        first = asyncio.create_task(ingest.save_message(photo(10, "a")))
        await asyncio.sleep(0.02)
        await ingest.save_message(photo(11))
        await first
        assert await result(2) == "version 1"
        return await status(1)

    assert run(tmp_path, scenario) == "CANCELLED"


def test_run_of_an_old_revision_exits_as_stale(tmp_path):
    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "a"))
        await ingest.save_message(photo(11))
        with SetWorkflowID("manual-old-rev"):
            return await processor.process(CHAT, True, ALBUM, 1)

    assert run(tmp_path, scenario) == "stale"
