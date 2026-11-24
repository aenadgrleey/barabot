"""Telegram transport configuration shared by setup and runtime."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dotenv import dotenv_values
from telethon import TelegramClient

TELEGRAM_ENV = "telegram.env"
_LEGACY_TELEGRAM_ENV = "scraper.env"


def telegram_env_path(config_dir: Path) -> Path:
    """`config/telegram.env`: API_ID and API_HASH of the Telegram application.

    A local scraper session and the bot both use them. Older installs kept
    them in scraper.env; the first call renames that file.
    """
    path = config_dir / TELEGRAM_ENV
    legacy = config_dir / _LEGACY_TELEGRAM_ENV
    if not path.exists() and legacy.exists():
        os.replace(legacy, path)
    return path


class TelegramBackend(StrEnum):
    TGMUX = "tgmux"
    TELETHON = "telethon"

    @classmethod
    def configured(cls, root: Path, environ: Mapping[str, str] | None = None) -> TelegramBackend:
        source = os.environ if environ is None else environ
        raw = source.get("TELEGRAM_BACKEND")
        if raw is None:
            raw = dotenv_values(root / "config" / ".env").get("TELEGRAM_BACKEND", cls.TGMUX)
        try:
            return cls((raw or "").strip())
        except ValueError:
            raise TelegramConfigError("TELEGRAM_BACKEND must be tgmux or telethon") from None

    def resolve(self) -> TelegramTransport:
        if self is TelegramBackend.TGMUX:
            try:
                from tgmux import SharedClient  # pyright: ignore[reportMissingImports]  # optional extra
            except ImportError as exc:
                logging.getLogger(__name__).warning("Using Telethon because tgmux import failed: %s", exc)
            else:
                return TelegramTransport(self, SharedClient)
        return TelegramTransport(TelegramBackend.TELETHON, TelegramClient)


class TelegramConfigError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramTransport:
    backend: TelegramBackend
    # SharedClient takes only a name, TelegramClient also takes the API credentials.
    client_type: Callable[..., TelegramClient]

    @property
    def uses_local_session(self) -> bool:
        return self.backend is TelegramBackend.TELETHON
