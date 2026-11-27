import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon.tl import types

from barabot.core import telegram
from barabot.core.db import connect, migrate
from barabot.domains.bot import subscriptions
from barabot.domains.scraper import service


def test_gateway_uses_registered_name_and_propagates_refusal(monkeypatch):
    client = AsyncMock()
    client.connect.side_effect = ConnectionError("gateway refused")
    client.is_connected = Mock(return_value=True)
    shared = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "tgmux", SimpleNamespace(SharedClient=shared))
    cfg = SimpleNamespace(telegram=telegram.TelegramBackend.TGMUX.resolve(), watch_chats=[])
    scraper = service.ScraperService(cfg, service.make_client(cfg), ingest=AsyncMock())

    async def scenario():
        with pytest.raises(ConnectionError, match="gateway refused"):
            await scraper.start()
        await scraper.stop()

    asyncio.run(scenario())
    shared.assert_called_once_with("barabot")
    client.disconnect.assert_awaited_once()


@pytest.mark.parametrize("module", [None, SimpleNamespace()], ids=["missing-package", "missing-client"])
def test_failed_optional_import_starts_local_telethon(monkeypatch, tmp_path, module):
    monkeypatch.setitem(sys.modules, "tgmux", module)

    session = tmp_path / "scraper.session"
    monkeypatch.setattr(service, "has_local_scraper_authorization", lambda path: path == session)
    client = AsyncMock()
    client.add_event_handler = Mock()
    client.is_connected = Mock(return_value=True)
    client.is_user_authorized.return_value = True
    constructor = Mock(return_value=client)
    monkeypatch.setattr(telegram, "TelegramClient", constructor)
    transport = telegram.TelegramBackend.TGMUX.resolve()
    assert transport.uses_local_session
    db = tmp_path / "barabot.sqlite"
    migrate(db)
    cfg = SimpleNamespace(
        telegram=transport,
        scraper_session_path=session,
        api_id=123,
        api_hash="a" * 32,
        watch_chats=[],
        db_path=db,
        backfill_hours=0,
    )
    scraper = service.ScraperService(cfg, service.make_client(cfg), ingest=AsyncMock())

    async def scenario():
        client.disconnected = asyncio.get_running_loop().create_future()
        await scraper.start()
        stop = asyncio.Event()
        stop.set()
        await scraper.run_forever(stop)
        await scraper.stop()

    asyncio.run(scenario())
    constructor.assert_called_once_with(str(session), cfg.api_id, cfg.api_hash)
    client.connect.assert_awaited_once()
    client.is_user_authorized.assert_awaited_once()
    assert client.add_event_handler.call_count == 3
    client.disconnect.assert_awaited_once()


def test_reads_a_chat_as_soon_as_the_database_marks_it_joined(tmp_path):
    db = tmp_path / "barabot.sqlite"
    migrate(db)
    subscriptions.add(db, user_id=1, author="a", query="q", chats=["@flats"])
    cfg = SimpleNamespace(db_path=db, watch_chats=[-1000000000999])
    ingest = AsyncMock()
    scraper = service.ScraperService(cfg, AsyncMock(), ingest=ingest)

    def message(chat_id):
        msg = types.Message(id=7, peer_id=types.PeerChannel(-chat_id - 1000000000000), message="сдаю", date=None)
        return SimpleNamespace(message=msg)

    async def scenario():
        await scraper._on_message(message(-1000000000111))  # not joined yet
        conn = connect(db)
        with conn:
            conn.execute("UPDATE chats SET status = 'joined', chat_id = -1000000000111")
        conn.close()
        await scraper._on_message(message(-1000000000111))
        await scraper._on_message(message(-1000000000999))  # from WATCH_CHATS

    asyncio.run(scenario())
    assert [call.args[0].chat_id for call in ingest.save_message.await_args_list] == [-1000000000111, -1000000000999]


def test_empty_document_is_described_without_crashing():
    media = types.MessageMediaDocument(document=types.DocumentEmpty(id=7))

    assert service.describe_media(media) == [{"type": "MessageMediaDocument"}]
