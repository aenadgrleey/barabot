import asyncio
import logging

import pytest
from telethon import errors, functions, types

from barabot.core.chats import ChatStatus
from barabot.core.db import connect, migrate
from barabot.domains.bot import subscriptions
from barabot.domains.scraper.joiner import ChatJoiner, JoinerService
from barabot.domains.scraper.service import joined_chat_ids

USER = 4242


def channel(id_, title, **flags):
    return types.Channel(id=id_, title=title, photo=types.ChatPhotoEmpty(), date=None, access_hash=1, **flags)


def folder(title, *chats):
    return types.chatlists.ChatlistInvite(
        title=types.TextWithEntities(text=title, entities=[]), peers=[], chats=list(chats), users=[]
    )


class FakeClient:
    """Telegram as the scraper account sees it."""

    def __init__(self):
        self.usernames = {"flats": channel(111, "Flats", megagroup=True)}
        self.ids = {-1000000000333: channel(333, "Old", megagroup=True, left=True)}
        self.invites = {"abc": channel(222, "Private", megagroup=True)}
        self.folders = {}
        self.dialog_filters = []
        self.folder_error = None
        self.member = set()
        self.approval_needed = set()
        self.flood = False
        self.requests = []

    async def get_entity(self, target):
        if isinstance(target, int):
            if target not in self.ids:
                raise ValueError("Could not find the input entity")
            return self.ids[target]
        if target not in self.usernames:
            raise ValueError(f'No user has "{target}" as username')
        return self.usernames[target]

    async def __call__(self, request):
        self.requests.append(request)
        if self.flood:
            raise errors.FloodWaitError(request, capture=42)
        if isinstance(request, functions.chatlists.CheckChatlistInviteRequest):
            result = self.folders.get(request.slug)
            if result is None:
                raise self.folder_error or errors.RPCError(request, "INVITE_SLUG_EXPIRED")
            return result
        if isinstance(request, functions.messages.GetDialogFiltersRequest):
            return types.messages.DialogFilters(filters=self.dialog_filters)
        if isinstance(request, functions.channels.JoinChannelRequest):
            chat_id = request.channel.id
            if chat_id in self.approval_needed:
                raise errors.InviteRequestSentError(request)
            if chat_id in self.member:
                raise errors.UserAlreadyParticipantError(request)
            self.member.add(chat_id)
            return types.Updates(updates=[], users=[], chats=[request.channel], date=None, seq=0)
        if isinstance(request, functions.messages.ImportChatInviteRequest):
            chat = self.invites.get(request.hash)
            if chat is None:
                raise errors.InviteHashExpiredError(request)
            if chat.id in self.member:
                raise errors.UserAlreadyParticipantError(request)
            self.member.add(chat.id)
            return types.Updates(updates=[], users=[], chats=[chat], date=None, seq=0)
        if isinstance(request, functions.messages.CheckChatInviteRequest):
            return types.ChatInviteAlready(chat=self.invites[request.hash])
        raise AssertionError(request)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "barabot.sqlite"
    migrate(path)
    return path


def chats(db):
    conn = connect(db)
    try:
        rows = conn.execute("SELECT kind, value, chat_id, title, status FROM chats ORDER BY id").fetchall()
        return {f"{r['kind']}:{r['value']}": (r["status"], r["chat_id"], r["title"]) for r in rows}
    finally:
        conn.close()


def run(db, client):
    """One round; returns how many chats it tried and every chat the database now marks joined."""
    joiner = ChatJoiner(db, client, pause=0)
    tried = asyncio.run(joiner.run_once())
    return tried, sorted(joined_chat_ids(db))


def test_joins_every_kind_of_chat_and_watches_it(db):
    subscriptions.add(
        db,
        user_id=USER,
        author="a",
        query="q",
        chats=["@flats", "https://t.me/+abc", "-1000000000333", "-1000000000444"],
    )
    client = FakeClient()
    client.ids[-1000000000444] = channel(444, "Watched", megagroup=True)  # a member already, via WATCH_CHATS
    tried, joined = run(db, client)

    assert tried == 4
    assert sorted(joined) == [-1000000000444, -1000000000333, -1000000000222, -1000000000111]
    assert chats(db) == {
        "username:flats": ("joined", -1000000000111, "Flats"),
        "invite:abc": ("joined", -1000000000222, "Private"),
        "id:-1000000000333": ("joined", -1000000000333, "Old"),
        # A member already: looked up, not joined again.
        "id:-1000000000444": ("joined", -1000000000444, "Watched"),
    }
    assert {type(r).__name__ for r in client.requests} == {"JoinChannelRequest", "ImportChatInviteRequest"}

    # Nothing left to do on the next round.
    assert run(db, client)[0] == 0


def test_already_a_member_counts_as_joined(db):
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["@flats", "https://t.me/+abc"])
    client = FakeClient()
    client.member = {111, 222}
    run(db, client)
    assert chats(db)["username:flats"][0] == chats(db)["invite:abc"][0] == "joined"
    assert chats(db)["invite:abc"][1] == -1000000000222


def test_missing_chats_fail_and_requests_wait(db):
    subscriptions.add(
        db, user_id=USER, author="a", query="q", chats=["@nosuchchat", "https://t.me/+gone", "-1000000000999"]
    )
    subscriptions.add(db, user_id=USER, author="b", query="q", chats=["@flats"])
    client = FakeClient()
    client.approval_needed = {111}
    tried, joined = run(db, client)

    assert tried == 4
    assert joined == []
    state = chats(db)
    assert state["username:nosuchchat"][0] == "failed"
    assert state["invite:gone"][0] == "failed"
    assert state["id:-1000000000999"][0] == "failed"
    assert state["username:flats"][0] == "requested"
    # Requested and failed chats wait before the next try.
    assert run(db, client)[0] == 0


def test_requested_chat_is_joined_once_approved(db):
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["@flats"])
    client = FakeClient()
    client.approval_needed = {111}
    run(db, client)
    conn = connect(db)
    conn.execute("UPDATE chats SET updated_at = datetime('now', '-1 hour')")
    conn.close()

    client.approval_needed = set()
    client.member = {111}  # the admin approved
    _, joined = run(db, client)
    assert joined == [-1000000000111]
    assert chats(db)["username:flats"][0] == "joined"


def test_only_chats_of_active_subscriptions_are_joined(db):
    sub_id = subscriptions.add(db, user_id=USER, author="a", query="q", chats=["@flats"])
    subscriptions.deactivate(db, sub_id, user_id=USER)
    assert run(db, FakeClient())[0] == 0
    assert chats(db)["username:flats"][0] == "pending"


def test_flood_wait_stops_the_round_and_keeps_the_chat_pending(db):
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["@flats"])
    client = FakeClient()
    client.flood = True
    with pytest.raises(errors.FloodWaitError):
        run(db, client)
    assert chats(db)["username:flats"][0] == ChatStatus.PENDING


def test_joined_chats_are_watched_after_a_restart(db):
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["@flats"])
    run(db, FakeClient())
    assert joined_chat_ids(db) == {-1000000000111}


def test_folder_link_expands_into_its_channels_once(db):
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["https://t.me/addlist/Flats_2026"])
    subscriptions.add(db, user_id=USER + 1, author="b", query="q", chats=["t.me/addlist/Flats_2026"])
    client = FakeClient()
    client.folders["Flats_2026"] = folder(
        "Flats folder",
        channel(111, "Flats", megagroup=True, username="flats"),
        channel(444, "NoUsername", megagroup=True),
        types.ChannelForbidden(id=555, access_hash=1, title="Secret"),
    )
    tried, joined = run(db, client)

    assert tried == 1
    assert sorted(joined) == [-1000000000444, -1000000000111]
    conn = connect(db)
    parent = conn.execute("SELECT id, status, title FROM chats WHERE kind = 'list'").fetchone()
    children = conn.execute(
        "SELECT kind, value, chat_id, title, status, parent_id FROM chats WHERE parent_id IS NOT NULL ORDER BY id"
    ).fetchall()
    links = conn.execute(
        "SELECT c.kind, c.value, count(*) FROM subscription_chats sc JOIN chats c ON c.id = sc.chat GROUP BY c.id"
    ).fetchall()
    conn.close()

    assert (parent["status"], parent["title"]) == ("expanded", "Flats folder")
    assert [tuple(r) for r in children] == [
        ("username", "flats", -1000000000111, "Flats", "joined", parent["id"]),
        ("id", "-1000000000444", -1000000000444, "NoUsername", "joined", parent["id"]),
    ]
    # Both subscriptions that named the folder name each extracted chat.
    assert {tuple(r) for r in links} == {
        ("list", "Flats_2026", 2),
        ("username", "flats", 2),
        ("id", "-1000000000444", 2),
    }

    # Nothing left to do on the next round, and no duplicated rows.
    assert run(db, client)[0] == 0
    conn = connect(db)
    assert conn.execute("SELECT count(*) FROM chats").fetchone()[0] == 3
    conn.close()


def test_folder_the_account_already_added_takes_its_title_from_the_accounts_folders(db):
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["https://t.me/addlist/Mine"])
    client = FakeClient()
    client.folders["Mine"] = types.chatlists.ChatlistInviteAlready(
        filter_id=7, missing_peers=[], already_peers=[], chats=[channel(111, "Flats", megagroup=True)], users=[]
    )
    client.dialog_filters = [
        types.DialogFilterDefault(),
        types.DialogFilterChatlist(
            id=7, title=types.TextWithEntities(text="My flats", entities=[]), pinned_peers=[], include_peers=[]
        ),
    ]
    run(db, client)
    assert chats(db)["list:Mine"] == ("expanded", None, "My flats")


def test_folder_telegram_rejects_fails_without_raising(db, caplog):
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["t.me/addlist/Gone"])

    with caplog.at_level(logging.WARNING):
        tried, joined = run(db, FakeClient())

    assert (tried, joined) == (1, [])
    assert chats(db)["list:Gone"] == ("failed", None, None)
    assert "INVITE_SLUG_EXPIRED" in caplog.text
    # The failed folder waits for FAILED_RETRY like any other failed chat.
    assert run(db, FakeClient())[0] == 0


def test_folder_unexpected_error_propagates(db):
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["t.me/addlist/Busy"])
    client = FakeClient()
    client.folder_error = errors.RPCError(None, "INTERNAL")

    with pytest.raises(errors.RPCError):
        run(db, client)

    # The folder stays pending, so the next round reads it again.
    assert chats(db)["list:Busy"] == ("pending", None, None)


def test_folder_without_channels_fails(db, caplog):
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["t.me/addlist/Empty"])
    client = FakeClient()
    client.folders["Empty"] = folder("Empty folder", types.ChannelForbidden(id=555, access_hash=1, title="Secret"))

    with caplog.at_level(logging.WARNING):
        run(db, client)

    assert chats(db)["list:Empty"] == ("failed", None, "Empty folder")
    assert "no channels" in caplog.text
    assert run(db, client)[0] == 0


def test_service_joins_until_stopped(db):
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["@flats"])
    joiner = JoinerService(db, FakeClient())

    async def scenario():
        await joiner.start()
        while not joined_chat_ids(db):
            await asyncio.sleep(0.01)
        stop = asyncio.Event()
        stop.set()
        await joiner.run_forever(stop)
        await joiner.stop()

    asyncio.run(asyncio.wait_for(scenario(), 5))
    assert chats(db)["username:flats"][0] == ChatStatus.JOINED


def _mark_joined_without_title(db, value, days_ago):
    conn = connect(db)
    try:
        conn.execute(
            "UPDATE chats SET status = 'joined', chat_id = CAST(value AS INTEGER), title = NULL, "
            "updated_at = datetime('now', ?) WHERE kind = 'id' AND value = ?",
            (f"-{days_ago} days", value),
        )
        conn.commit()
    finally:
        conn.close()


def test_joined_chat_without_title_gets_its_title(db):
    # Older code marked WATCH_CHATS joined without asking Telegram, so they had no title.
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["-1000000000333"])
    _mark_joined_without_title(db, "-1000000000333", days_ago=3)
    client = FakeClient()

    tried, joined = run(db, client)

    assert tried == 1
    assert joined == [-1000000000333]
    assert chats(db)["id:-1000000000333"] == ("joined", -1000000000333, "Old")
    assert client.requests == []  # looked up, not joined
    assert run(db, client)[0] == 0


def test_failed_title_lookup_keeps_the_chat_joined_and_waits(db):
    subscriptions.add(db, user_id=USER, author="a", query="q", chats=["-1000000000555"])
    _mark_joined_without_title(db, "-1000000000555", days_ago=3)
    client = FakeClient()  # does not know chat 555

    assert run(db, client) == (1, [-1000000000555])
    assert chats(db)["id:-1000000000555"] == ("joined", -1000000000555, None)
    assert run(db, client)[0] == 0  # not asked again for a day


def test_adding_a_folder_later_links_the_chats_already_watched(db):
    # The chats were named one by one before; the folder link names the same chats.
    sub_id = subscriptions.add(db, user_id=USER, author="a", query="q", chats=["@flats"])
    client = FakeClient()
    client.usernames["flats"] = channel(111, "Flats", megagroup=True, username="flats")
    run(db, client)
    client.folders["Flats_2026"] = folder("Flats folder", channel(111, "Flats", megagroup=True, username="flats"))
    subscriptions.update(db, sub_id, user_id=USER, chats=["@flats", "t.me/addlist/Flats_2026"])

    run(db, client)

    [row] = subscriptions.list_all(db, user_id=USER)
    assert [(str(c.ref), c.status.value, str(c.folder) if c.folder else None) for c in row.chats] == [
        ("@flats", "joined", "https://t.me/addlist/Flats_2026"),
        ("https://t.me/addlist/Flats_2026", "expanded", None),
    ]
