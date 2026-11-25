"""Offline setup guidance based only on expected file existence."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from dotenv import dotenv_values

from barabot.core.telegram import TelegramBackend, TelegramTransport, telegram_env_path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

SETUP_COMMAND = ".venv/bin/python -m barabot setup"
SCRAPER_COMMAND = ".venv/bin/python -m barabot setup scraper"
PROCESSOR_COMMAND = ".venv/bin/python -m barabot processor configure"
BOT_COMMAND = ".venv/bin/python -m barabot bot auth <token>"
READY_COMMAND = ".venv/bin/python -m barabot run --check"
# No setup command writes it yet; the hint names the file and where it is described.
MCP_HINT = "write config/mcp.env (README: MCP connector)"


def has_local_scraper_authorization(path: Path) -> bool:
    """Check Telethon's SQLite auth key without network or file mutation."""
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        uri = f"file:{path.resolve()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE auth_key IS NOT NULL AND length(auth_key) > 0 LIMIT 1"
            ).fetchone()
    except (OSError, sqlite3.Error):
        return False
    return row is not None


def has_deploy_environment(path: Path) -> bool:
    return bool(dotenv_values(path).get("BARABOT_ENV"))


def missing_setup_commands(root: Path, transport: TelegramTransport | None = None) -> list[str]:
    """Return setup commands for artifacts absent below ``root``."""
    if transport is None:
        transport = TelegramBackend.configured(root).resolve()
    config_dir = root / "config"
    commands: list[str] = []

    has_telegram_app = telegram_env_path(config_dir).exists()
    if not has_deploy_environment(config_dir / ".env") or (not has_telegram_app and not transport.uses_local_session):
        commands.append(SETUP_COMMAND)
    if transport.uses_local_session and (
        not has_telegram_app or not has_local_scraper_authorization(config_dir / "scraper.session")
    ):
        commands.append(SCRAPER_COMMAND)
    if not (config_dir / "processor.env").exists():
        commands.append(PROCESSOR_COMMAND)
    if not (config_dir / "bot.token").exists():
        commands.append(BOT_COMMAND)
    if not (config_dir / "mcp.env").exists():
        commands.append(MCP_HINT)

    return commands


def next_setup_command(root: Path, transport: TelegramTransport | None = None) -> str:
    """Return the next missing component command, or the readiness check."""
    commands = missing_setup_commands(root, transport)
    return commands[0] if commands else READY_COMMAND


def setup_guidance_commands(root: Path, transport: TelegramTransport | None = None) -> list[str]:
    """Return resumable setup guidance with the orchestrator first."""
    missing = missing_setup_commands(root, transport)
    if not missing:
        return []
    return list(dict.fromkeys([SETUP_COMMAND, *missing]))
