import asyncio

import pytest

from barabot.api import UNSET, BarabotApi, ChatId, ChatStatus, InvalidRequest, SubscriptionNotFound, Username
from barabot.core.db import ListingKey, connect, migrate

RENT = {"author": "сдаёт жильё", "author_not": "ищет жильё", "query": "квартира с балконом"}


@pytest.fixture
def api(tmp_path):
    path = tmp_path / "barabot.sqlite"
    migrate(path)
    return BarabotApi(path)


def run(coro):
    return asyncio.run(coro)


def test_add_saves_chats_as_named_and_pending(api):
    sub = run(api.add_subscription(5, chats=["https://t.me/Prague_Flats", "-1001234567890"], **RENT))
    assert (sub.user_id, sub.author, sub.author_not, sub.query, sub.revision) == (
        5,
        "сдаёт жильё",
        "ищет жильё",
        "квартира с балконом",
        1,
    )
    assert [(c.ref, c.chat_id, c.status) for c in sub.chats] == [
        (Username("prague_flats"), None, ChatStatus.PENDING),
        (ChatId(-1001234567890), -1001234567890, ChatStatus.PENDING),
    ]


@pytest.mark.parametrize(
    "change",
    [{"query": "  "}, {"author": ""}, {"chats": []}, {"chats": ["https://example.com/chat"]}],
)
def test_add_rejects_bad_input_and_saves_nothing(api, change):
    with pytest.raises(InvalidRequest):
        run(api.add_subscription(5, **{**RENT, "chats": ["@prague_flats"], **change}))
    assert run(api.list_subscriptions(5)) == []


def test_list_shows_only_own_active_subscriptions(api):
    mine = run(api.add_subscription(5, chats=["@prague_flats"], **RENT))
    gone = run(api.add_subscription(5, chats=["@prague_flats"], **RENT))
    run(api.add_subscription(6, chats=["@prague_flats"], **RENT))
    run(api.remove_subscription(5, gone.id))
    assert [s.id for s in run(api.list_subscriptions(5))] == [mine.id]


def test_update_changes_only_given_fields(api):
    sub = run(api.add_subscription(5, chats=["@prague_flats"], **RENT))
    updated = run(api.update_subscription(5, sub.id, query="квартира у метро"))
    assert (updated.author, updated.author_not, updated.query) == ("сдаёт жильё", "ищет жильё", "квартира у метро")
    assert updated.revision == 2
    assert [c.ref for c in updated.chats] == [Username("prague_flats")]


def test_none_clears_author_not(api):
    sub = run(api.add_subscription(5, chats=["@prague_flats"], **RENT))
    assert run(api.update_subscription(5, sub.id, author_not=None)).author_not is None


def test_chats_or_same_text_keep_the_revision(api):
    sub = run(api.add_subscription(5, chats=["@prague_flats"], **RENT))
    same = run(api.update_subscription(5, sub.id, author=" сдаёт жильё ", query=RENT["query"]))
    moved = run(api.update_subscription(5, sub.id, chats=["@brno_flats", "-100777"]))
    assert same.revision == moved.revision == 1
    assert [c.ref for c in moved.chats] == [Username("brno_flats"), ChatId(-100777)]


@pytest.mark.parametrize("change", [{"query": ""}, {"chats": []}, {"chats": ["@x"]}])
def test_update_rejects_bad_input_and_keeps_the_subscription(api, change):
    sub = run(api.add_subscription(5, chats=["@prague_flats"], **RENT))
    with pytest.raises(InvalidRequest):
        run(api.update_subscription(5, sub.id, **change))
    assert run(api.get_subscription(5, sub.id)) == sub


def test_update_without_changes_returns_the_subscription(api):
    sub = run(api.add_subscription(5, chats=["@prague_flats"], **RENT))
    assert run(api.update_subscription(5, sub.id, author=UNSET)) == sub


def test_someone_elses_or_removed_subscription_is_not_found(api):
    sub = run(api.add_subscription(5, chats=["@prague_flats"], **RENT))
    for call in (
        api.get_subscription(6, sub.id),
        api.update_subscription(6, sub.id, query="другое"),
        api.remove_subscription(6, sub.id),
    ):
        with pytest.raises(SubscriptionNotFound):
            run(call)
    run(api.remove_subscription(5, sub.id))
    with pytest.raises(SubscriptionNotFound):
        run(api.update_subscription(5, sub.id, query="другое"))
    with pytest.raises(SubscriptionNotFound):
        run(api.remove_subscription(5, sub.id))


def test_processing_finds_subscriptions_once_the_chat_is_resolved(api, tmp_path):
    from barabot.domains.processor.listing import ListingProcessor

    sub = run(api.add_subscription(5, chats=["@prague_flats", "-100777"], **RENT))
    conn = connect(tmp_path / "barabot.sqlite")
    conn.execute(
        "UPDATE chats SET chat_id = -100777, status = 'joined' WHERE kind = 'username' AND value = 'prague_flats'"
    )
    conn.execute("INSERT INTO listings (chat_id, album, ref, rev, version) VALUES (-100777, 0, 1, 1, 1)")
    conn.close()
    processor = ListingProcessor(tmp_path / "barabot.sqlite", tmp_path, decider=None, notifier=None, name=tmp_path.name)
    found = processor._load_subscriptions_blocking(ListingKey(-100777, False, 1), 1, 1)
    assert found is not None
    assert [s.id for s in found] == [sub.id]
