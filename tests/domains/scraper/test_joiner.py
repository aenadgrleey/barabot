import asyncio

import pytest
from telethon import errors, functions, types

from barabot.core.chats import ChatStatus
from barabot.core.db import connect, migrate
from barabot.domains.bot import subscriptions
from barabot.domains.scraper.joiner import ChatJoiner

USER = 4242


def channel(id_, title, **flags):
    return types.Channel(id=id_, title=title, photo=types.ChatPhotoEmpty(), date=None, access_hash=1, **flags)


class FakeClient:
    """Telegram as the scraper account sees it."""

    def __init__(self):
        self.usernames = {"flats": channel(111, "Flats", megagroup=True)}
        self.ids = {-1000000000333: channel(333, "Old", megagroup=True, left=True)}
        self.invites = {"abc": channel(222, "Private", megagroup=True)}
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


def run(db, client, watched=()):
    watched = set(watched)
    joined = []

    def on_joined(chat_id):
        watched.add(chat_id)
        joined.append(chat_id)

    joiner = ChatJoiner(db, client, watched, on_joined, pause=0)
    tried = asyncio.run(joiner.run_once())
    return tried, joined


def test_joins_every_kind_of_chat_and_watches_it(db):
    subscriptions.add(
        db,
        user_id=USER,
        author="a",
        query="q",
        chats=["@flats", "https://t.me/+abc", "-1000000000333", "-1000000000444"],
    )
    client = FakeClient()
    tried, joined = run(db, client, watched=[-1000000000444])

    assert tried == 4
    assert sorted(joined) == [-1000000000444, -1000000000333, -1000000000222, -1000000000111]
    assert chats(db) == {
        "username:flats": ("joined", -1000000000111, "Flats"),
        "invite:abc": ("joined", -1000000000222, "Private"),
        "id:-1000000000333": ("joined", -1000000000333, "Old"),
        # In WATCH_CHATS already: no request to Telegram.
        "id:-1000000000444": ("joined", -1000000000444, None),
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
    joiner = ChatJoiner(db, FakeClient(), set(), lambda _: None)
    assert joiner.joined_chat_ids() == {-1000000000111}
