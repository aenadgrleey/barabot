"""Process-wide configuration loaded from layered private env files."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import ClassVar

from dotenv import load_dotenv

from barabot.core.telegram import TelegramBackend, TelegramTransport

# barabot/core/config.py → barabot/core → barabot → <project>
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

DATA_DIR = PROJECT_ROOT / "data"

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class DeployEnvironment(Enum):
    develop = "develop"
    prod = "prod"


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"{name} is missing. Copy .env.example to .env and fill it in."
        )
    return value


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _list(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Config:
    bot_token_path: ClassVar[Path] = PROJECT_ROOT / "config" / "bot.token"

    telegram: TelegramTransport
    environment: DeployEnvironment
    api_id: int | None
    api_hash: str | None
    watch_chats: list[int]
    log_level: str
    backfill_hours: int
    typesafe_api_key: str

    scraper_session_path: Path = field(
        default_factory=lambda: PROJECT_ROOT / "config" / "scraper.session"
    )
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot.sqlite")
    dbos_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "dbos.sqlite")
    media_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "media")
    lock_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot.lock")
    pid_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot.pid")


def _validate_api_hash(value: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise RuntimeError(
            "API_HASH must be 32 hex characters (get it from https://my.telegram.org)"
        )


def load() -> Config:
    transport = TelegramBackend.configured(PROJECT_ROOT).resolve()
    if transport.uses_local_session:
        load_dotenv(PROJECT_ROOT / "config" / "scraper.env", override=False)
    load_dotenv(PROJECT_ROOT / "config" / "processor.env", override=False)
    load_dotenv(PROJECT_ROOT / "config" / ".env", override=False)

    raw_environment = os.getenv("BARABOT_ENV", "").strip()
    accepted_environments = "develop, prod"
    if not raw_environment:
        raise RuntimeError(
            f"BARABOT_ENV is missing; accepted values: {accepted_environments}"
        )
    try:
        environment = DeployEnvironment(raw_environment)
    except ValueError as exc:
        raise RuntimeError(
            f"BARABOT_ENV has invalid value {raw_environment!r}; "
            f"accepted values: {accepted_environments}"
        ) from exc

    api_id = None
    api_hash = None
    if transport.uses_local_session:
        api_id = int(_required("API_ID"))
        api_hash = _required("API_HASH")
        _validate_api_hash(api_hash)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise RuntimeError(
            f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {log_level!r}"
        )
    backfill_hours = _int("BACKFILL_HOURS", 24)
    if backfill_hours < 0:
        raise RuntimeError("BACKFILL_HOURS must be >= 0")

    return Config(
        telegram=transport,
        environment=environment,
        api_id=api_id,
        api_hash=api_hash,
        watch_chats=_list("WATCH_CHATS"),
        log_level=log_level,
        backfill_hours=backfill_hours,
        typesafe_api_key=_required("TYPESAFE_API_KEY"),
    )
