import pytest


@pytest.fixture(autouse=True)
def standalone_telegram(monkeypatch):
    """Exercise standalone setup even when the optional client is installed."""
    monkeypatch.setenv("TELEGRAM_BACKEND", "telethon")
