import asyncio
from types import SimpleNamespace

from telethon import errors, functions, types

from barabot.domains.scraper import admin


class FakeClient:
    def __init__(self, entities, already=()):
        self.entities = entities
        self.already = set(already)
        self.joined = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_entity(self, target):
        return self.entities[target]

    async def __call__(self, request):
        assert isinstance(request, functions.channels.JoinChannelRequest)
        if request.channel.id in self.already:
            raise errors.UserAlreadyParticipantError(request)
        self.joined.append(request.channel.id)


def channel(id_, title, **flags):
    return types.Channel(id=id_, title=title, photo=types.ChatPhotoEmpty(), date=None, access_hash=1, **flags)


def test_join_reports_marked_ids_for_watch_chats(monkeypatch):
    client = FakeClient(
        {
            "NSbaraholka": channel(111, "NS baraholka", megagroup=True),
            "serbska_baraholka": channel(222, "Serbska", broadcast=True),
        },
        already={222},
    )
    monkeypatch.setattr(admin, "make_client", lambda cfg: client)
    cfg = SimpleNamespace(telegram=SimpleNamespace(backend="tgmux", uses_local_session=False))
    result = asyncio.run(admin.join(cfg, ["NSbaraholka", "https://t.me/serbska_baraholka"]))
    assert [(c["id"], c["kind"], c["status"]) for c in result] == [
        (-1000000000111, "supergroup", "joined"),
        (-1000000000222, "channel", "already a member"),
    ]
    assert client.joined == [111]
