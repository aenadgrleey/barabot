"""Config for the reporter bot. Independent from barabot.config — the
two processes have different credentials and different settings."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


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


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw else default


def _list(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    bot_token: str
    log_level: str
    poll_interval: float
    allowed_chat_ids: list[int]
    default_limit: int

    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot.sqlite")
    lock_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot-reporter.lock")
    pid_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot-reporter.pid")


def _validate_log_level(value: str) -> None:
    if value not in _VALID_LOG_LEVELS:
        raise RuntimeError(
            f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {value!r}"
        )


def load() -> Config:
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    _validate_log_level(log_level)
    poll = _float("REPORTER_POLL_INTERVAL", 2.0)
    if poll < 0.5:
        raise RuntimeError("REPORTER_POLL_INTERVAL must be >= 0.5s")
    default_limit = _int("REPORTER_DEFAULT_LIMIT", 10)
    if default_limit < 1 or default_limit > 100:
        raise RuntimeError("REPORTER_DEFAULT_LIMIT must be in [1, 100]")

    return Config(
        api_id=int(_required("API_ID")),
        api_hash=_required("API_HASH"),
        bot_token=_required("REPORTER_BOT_TOKEN"),
        log_level=log_level,
        poll_interval=poll,
        allowed_chat_ids=_list("ALLOWED_CHAT_IDS"),
        default_limit=default_limit,
    )
