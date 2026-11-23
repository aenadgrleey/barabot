import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from barabot.core import telegram
from barabot.domains.scraper import service


def test_gateway_uses_registered_name_and_propagates_refusal(monkeypatch):
    client = AsyncMock()
    client.connect.side_effect = ConnectionError("gateway refused")
    shared = Mock(return_value=client)
    monkeypatch.setitem(sys.modules, "tgmux", SimpleNamespace(SharedClient=shared))
    scraper = service.ScraperService(SimpleNamespace(telegram=telegram.TelegramBackend.TGMUX.resolve()))

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
    client.is_user_authorized.return_value = True
    constructor = Mock(return_value=client)
    monkeypatch.setattr(telegram, "TelegramClient", constructor)
    transport = telegram.TelegramBackend.TGMUX.resolve()
    assert transport.uses_local_session
    cfg = SimpleNamespace(telegram=transport, scraper_session_path=session, api_id=123, api_hash="a" * 32)
    scraper = service.ScraperService(cfg)

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
    client.disconnect.assert_awaited_once()
