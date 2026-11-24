"""ListingProcessor on a real DBOS with SQLite in tmp_path, and a fake Jev.

The debounce is shortened to keep the suite fast; queue polling is 0.2 s.
FakeDecider answers by rules and records every call, so the tests see
which subscriptions went to which round.
"""
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx2
from dbos import DBOS, SetWorkflowID
from typesafe_sdk import TypeSafeBadRequestError

from barabot.core import durable
from barabot.core.db import ListingKey, connect, migrate
from barabot.domains.bot import subscriptions
from barabot.domains.processor.listing import ListingProcessor
from barabot.domains.processor.matching import Decision
from barabot.domains.scraper.ingest import MessageIngest
from barabot.domains.scraper.model.event import DeletedMessages, SeenMessage

CHAT = -1001234567890
OTHER_CHAT = -1009999999999
ALBUM = 13_000_000_000_000_001
KEY = ListingKey(CHAT, True, ALBUM)


class FakeDecider:
    """Text round: `text_answers[query]`; photo round: `photo_answers[query]`."""

    def __init__(self, text_answers=None, photo_answers=None, reject_photos=False):
        self.text_answers = text_answers or {}
        self.photo_answers = photo_answers or {}
        self.reject_photos = reject_photos
        self.calls: list[tuple[str, list[int], list[str]]] = []

    async def decide(self, text, subs, photos=()):
        self.calls.append((text, [s.id for s in subs], [Path(p).name for p in photos]))
        if photos and self.reject_photos:
            raise TypeSafeBadRequestError(400, {"detail": "image rejected"}, httpx2.Headers())
        answers = self.photo_answers if photos else self.text_answers
        return [
            Decision(s.id, 0.95, answers.get(s.query, "does_not_fit"), 0.9, 2 if photos else 1)
            for s in subs
        ]


def run(tmp_path: Path, scenario, decider=None, subs=()):
    db = tmp_path / "barabot.sqlite"
    media = tmp_path / "media"
    migrate(db)
    for query, chats, active in subs:
        sub_id = subscriptions.add(db, user_id=1, author="сдаёт жильё", query=query, chats=chats)
        if not active:
            subscriptions.deactivate(db, sub_id)
    decider = decider or FakeDecider()

    # One instance per config_name per process, so every test gets its own.
    processor = ListingProcessor(db, media, decider, debounce_seconds=0.3, name=tmp_path.name)

    async def main():
        durable.launch(tmp_path / "dbos.sqlite")
        try:
            await processor.open()
            return await scenario(db, processor, MessageIngest(db, processor.schedule))
        finally:
            durable.shutdown()

    return asyncio.run(main())


def photo(msg_id, text="", photo_id=None, file=None) -> SeenMessage:
    item = {"type": "photo", "id": photo_id or msg_id}
    if file:
        item["file"] = file
    return SeenMessage(CHAT, msg_id, ALBUM, 42, datetime(2026, 11, 23, tzinfo=UTC), text, [item])


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


def matches(db: Path) -> dict[int, tuple]:
    conn = connect(db)
    try:
        return {
            r["subscription_id"]: (r["version"], r["choice"], r["round"], r["matched"])
            for r in conn.execute("SELECT * FROM matches")
        }
    finally:
        conn.close()


# --- versions and debounce -------------------------------------------------


def test_album_burst_runs_only_the_last_revision(tmp_path):
    async def scenario(db, processor, ingest):
        for msg in (photo(10, "2 rooms, 900 EUR"), photo(11), photo(12)):
            await ingest.save_message(msg)
        assert await result(3) == "version 1: 0 match(es)"
        return [await status(rev) for rev in (1, 2)]

    assert run(tmp_path, scenario) == ["CANCELLED", "CANCELLED"]
    listing_row = row(tmp_path / "barabot.sqlite")
    assert listing_row["text"] == "2 rooms, 900 EUR"
    assert [m["id"] for m in json.loads(listing_row["media"])] == [10, 11, 12]
    assert (listing_row["version"], listing_row["removed"], listing_row["decided_version"]) == (1, 0, 1)


def test_retouch_without_change_keeps_the_version(tmp_path):
    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "caption"))
        assert await result(1) == "version 1: 0 match(es)"
        await ingest.save_message(photo(10, "caption"))
        return await result(2)

    assert run(tmp_path, scenario) == "unchanged"
    assert row(tmp_path / "barabot.sqlite")["version"] == 1


def test_edit_and_delete_produce_new_versions(tmp_path):
    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "900 EUR"))
        await ingest.save_message(photo(11))
        assert await result(2) == "version 1: 0 match(es)"
        await ingest.save_message(photo(10, "850 EUR"))
        assert await result(3) == "version 2: 0 match(es)"
        assert row(db)["text"] == "850 EUR"
        await ingest.mark_deleted(DeletedMessages(CHAT, [10, 11]))
        return await result(4)

    assert run(tmp_path, scenario) == "version 3: 0 match(es)"
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
        assert await result(2) == "version 1: 0 match(es)"
        return await status(1)

    assert run(tmp_path, scenario) == "CANCELLED"


def test_run_of_an_old_revision_exits_as_stale(tmp_path):
    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "a"))
        await ingest.save_message(photo(11))
        with SetWorkflowID("manual-old-rev"):
            return await processor.process(CHAT, True, ALBUM, 1)

    assert run(tmp_path, scenario) == "stale"


# --- matching ---------------------------------------------------------------


def test_matches_are_saved_and_the_version_marked_decided(tmp_path):
    decider = FakeDecider(text_answers={"квартира с балконом": "fits"})

    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "Сдаю 2+kk с балконом"))
        return await result(1)

    subs = [("квартира с балконом", [CHAT], True), ("комната", [CHAT], True)]
    assert run(tmp_path, scenario, decider, subs) == "version 1: 1 match(es)"
    db = tmp_path / "barabot.sqlite"
    assert matches(db) == {1: (1, "fits", 1, 1), 2: (1, "does_not_fit", 1, 0)}
    assert row(db)["decided_version"] == 1
    assert decider.calls == [("Сдаю 2+kk с балконом", [1, 2], [])]


def test_unclear_goes_to_a_photo_round_for_those_subscriptions_only(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "777-x.jpg").write_bytes(b"jpeg")
    decider = FakeDecider(
        text_answers={"квартира с балконом": "fits", "квартира с огромными окнами": "unclear"},
        photo_answers={"квартира с огромными окнами": "fits"},
    )

    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "Сдаю 2+kk, подробности на фото", photo_id=777, file="777-x.jpg"))
        return await result(1)

    subs = [("квартира с балконом", [CHAT], True), ("квартира с огромными окнами", [CHAT], True)]
    assert run(tmp_path, scenario, decider, subs) == "version 1: 2 match(es)"
    assert decider.calls[1] == ("Сдаю 2+kk, подробности на фото", [2], ["777-x.jpg"])
    assert matches(tmp_path / "barabot.sqlite") == {1: (1, "fits", 1, 1), 2: (1, "fits", 2, 1)}


def test_unclear_without_a_downloaded_photo_stays_text_only(tmp_path):
    decider = FakeDecider(text_answers={"квартира с огромными окнами": "unclear"})

    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "Сдаю, подробности на фото"))  # no file
        return await result(1)

    subs = [("квартира с огромными окнами", [CHAT], True)]
    assert run(tmp_path, scenario, decider, subs) == "version 1: 0 match(es)"
    assert len(decider.calls) == 1
    assert matches(tmp_path / "barabot.sqlite") == {1: (1, "unclear", 1, 0)}


def test_rejected_photos_keep_the_text_answers(tmp_path):
    media = tmp_path / "media"
    media.mkdir()
    (media / "777-x.jpg").write_bytes(b"not really a jpeg")
    decider = FakeDecider(text_answers={"квартира с огромными окнами": "unclear"}, reject_photos=True)

    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "Сдаю", photo_id=777, file="777-x.jpg"))
        return await result(1)

    subs = [("квартира с огромными окнами", [CHAT], True)]
    assert run(tmp_path, scenario, decider, subs) == "version 1: 0 match(es)"
    assert matches(tmp_path / "barabot.sqlite") == {1: (1, "unclear", 1, 0)}


def test_only_active_subscriptions_of_the_listing_chat_are_asked(tmp_path):
    decider = FakeDecider()

    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "Сдаю 2+kk"))
        return await result(1)

    subs = [("квартира", [OTHER_CHAT], True), ("комната", [CHAT], False), ("студия", [CHAT, OTHER_CHAT], True)]
    run(tmp_path, scenario, decider, subs)
    assert decider.calls == [("Сдаю 2+kk", [3], [])]


def test_no_subscriptions_means_no_request(tmp_path):
    decider = FakeDecider()

    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "Сдаю 2+kk"))
        return await result(1)

    assert run(tmp_path, scenario, decider) == "version 1: 0 match(es)"
    assert decider.calls == []


def test_a_saved_but_undecided_version_is_decided_by_the_next_run(tmp_path):
    """A run stopped after save_version: the next run must not call it unchanged."""
    decider = FakeDecider(text_answers={"квартира": "fits"})

    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "Сдаю 2+kk"))
        assert await result(1) == "version 1: 1 match(es)"
        conn = connect(db)
        conn.execute("UPDATE listings SET decided_version = 0")
        conn.execute("DELETE FROM matches")
        conn.close()
        await ingest.save_message(photo(10, "Сдаю 2+kk"))  # same content, e.g. backfill
        return await result(2)

    assert run(tmp_path, scenario, decider, [("квартира", [CHAT], True)]) == "version 1: 1 match(es)"
    assert row(tmp_path / "barabot.sqlite")["decided_version"] == 1
    assert len(decider.calls) == 2


def test_removed_listing_is_not_sent_to_jev(tmp_path):
    decider = FakeDecider(text_answers={"квартира": "fits"})

    async def scenario(db, processor, ingest):
        await ingest.save_message(photo(10, "Сдаю 2+kk"))
        assert await result(1) == "version 1: 1 match(es)"
        await ingest.mark_deleted(DeletedMessages(CHAT, [10]))
        return await result(2)

    assert run(tmp_path, scenario, decider, [("квартира", [CHAT], True)]) == "version 2: 0 match(es)"
    assert len(decider.calls) == 1
