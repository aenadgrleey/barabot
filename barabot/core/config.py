"""Process-wide configuration loaded from layered private env files."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlsplit

from dotenv import load_dotenv

from barabot.core.telegram import TelegramBackend, TelegramTransport, telegram_env_path

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
        raise RuntimeError(f"{name} is missing. Copy .env.example to .env and fill it in.")
    return value


def _float(name: str, default: float | None) -> float | None:
    raw = os.getenv(name, "").strip()
    return float(raw) if raw else default


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw else default


def _list(name: str) -> list[int]:
    raw = os.getenv(name, "").strip()
    if not raw:
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class WebConfig:
    """The HTTP server behind PUBLIC_URL, from config/web.env. It serves the MCP connector and the Mini App."""

    public_url: str  # PUBLIC_URL: HTTPS origin that clients and Telegram see, no trailing slash
    port: int  # WEB_PORT: local port behind the HTTPS proxy, bound to 127.0.0.1

    @property
    def mcp_url(self) -> str:
        """What a user pastes into their MCP client: the public URL with /mcp."""
        return self.public_url + "/mcp"


@dataclass(frozen=True)
class McpConfig:
    """Log In With Telegram for the MCP connector, from config/mcp.env. Required."""

    login_client_id: str  # from @BotFather > Login Widget
    login_client_secret: str


def _file_value(name: str, file: str, readme: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is missing. Write it to config/{file}, see README: {readme}.")
    return value


def _web() -> WebConfig:
    old = [name for name in ("MCP_PUBLIC_URL", "MCP_PORT") if os.getenv(name)]
    if old and not (os.getenv("PUBLIC_URL") and os.getenv("WEB_PORT")):
        raise RuntimeError(
            f"{' and '.join(old)} moved: write PUBLIC_URL and WEB_PORT to config/web.env "
            "and remove the old names from config/mcp.env."
        )
    public_url = _file_value("PUBLIC_URL", "web.env", "Web server").rstrip("/")
    if not public_url.startswith("https://"):
        raise RuntimeError("PUBLIC_URL must be an https:// URL")
    # Claude connects from Anthropic's servers and refuses a URL that names a port.
    if re.search(r":\d*$", urlsplit(public_url).netloc):
        raise RuntimeError(
            f"PUBLIC_URL must not name a port, got {public_url!r}. "
            "Claude only connects to the standard HTTPS port 443; publish the server there."
        )
    raw_port = _file_value("WEB_PORT", "web.env", "Web server")
    if not raw_port.isdigit() or not 0 < int(raw_port) < 65536:
        raise RuntimeError(f"WEB_PORT must be a TCP port number, got {raw_port!r}")
    return WebConfig(public_url=public_url, port=int(raw_port))


def _mcp() -> McpConfig:
    return McpConfig(
        login_client_id=_file_value("TELEGRAM_LOGIN_CLIENT_ID", "mcp.env", "MCP connector"),
        login_client_secret=_file_value("TELEGRAM_LOGIN_CLIENT_SECRET", "mcp.env", "MCP connector"),
    )


@dataclass(frozen=True)
class Config:
    bot_token_path: ClassVar[Path] = PROJECT_ROOT / "config" / "bot.token"

    telegram: TelegramTransport
    environment: DeployEnvironment
    api_id: int  # with api_hash: the Telegram application of the bot and a local scraper session
    api_hash: str
    watch_chats: list[int]
    log_level: str
    backfill_hours: int
    typesafe_api_key: str
    jev_budget_usd: float | None  # hard spend limit on Jev; None = unlimited
    jev_price_per_m_input: float  # USD per million input tokens, used to compute spend
    stats_interval_minutes: int  # digest period; 0 turns the digest off
    web: WebConfig
    mcp: McpConfig

    scraper_session_path: Path = field(default_factory=lambda: PROJECT_ROOT / "config" / "scraper.session")
    bot_session_path: Path = field(default_factory=lambda: PROJECT_ROOT / "config" / "bot.session")
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot.sqlite")
    dbos_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "dbos.sqlite")
    media_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "media")
    lock_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot.lock")
    pid_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "barabot.pid")


def _validate_api_hash(value: str) -> None:
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise RuntimeError("API_HASH must be 32 hex characters (get it from https://my.telegram.org)")


def _price() -> float:
    price = _float("JEV_PRICE_PER_M_INPUT", None)
    return 0.042 if price is None else price


def _non_negative(value, name: str):
    if value is not None and value < 0:
        raise RuntimeError(f"{name} must be >= 0")
    return value


def load() -> Config:
    transport = TelegramBackend.configured(PROJECT_ROOT).resolve()
    # API_ID and API_HASH: the bot's Telethon client always needs them, a
    # local scraper session too.
    load_dotenv(telegram_env_path(PROJECT_ROOT / "config"), override=False)
    load_dotenv(PROJECT_ROOT / "config" / "processor.env", override=False)
    load_dotenv(PROJECT_ROOT / "config" / "web.env", override=False)
    load_dotenv(PROJECT_ROOT / "config" / "mcp.env", override=False)
    load_dotenv(PROJECT_ROOT / "config" / ".env", override=False)

    raw_environment = os.getenv("BARABOT_ENV", "").strip()
    accepted_environments = "develop, prod"
    if not raw_environment:
        raise RuntimeError(f"BARABOT_ENV is missing; accepted values: {accepted_environments}")
    try:
        environment = DeployEnvironment(raw_environment)
    except ValueError as exc:
        raise RuntimeError(
            f"BARABOT_ENV has invalid value {raw_environment!r}; accepted values: {accepted_environments}"
        ) from exc

    api_id = int(_required("API_ID"))
    api_hash = _required("API_HASH")
    _validate_api_hash(api_hash)
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    if log_level not in _VALID_LOG_LEVELS:
        raise RuntimeError(f"LOG_LEVEL must be one of {sorted(_VALID_LOG_LEVELS)}, got {log_level!r}")
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
        jev_budget_usd=_non_negative(_float("JEV_BUDGET_USD", None), "JEV_BUDGET_USD"),
        jev_price_per_m_input=_non_negative(_price(), "JEV_PRICE_PER_M_INPUT"),
        stats_interval_minutes=_non_negative(_int("STATS_INTERVAL_MINUTES", 60), "STATS_INTERVAL_MINUTES"),
        web=_web(),
        mcp=_mcp(),
    )
