import sqlite3
from pathlib import Path

from barabot.core import setup_hints


def _authorized_session(root: Path) -> None:
    path = root / "config/scraper.session"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, server_address TEXT, "
            "port INTEGER, auth_key BLOB, takeout_id INTEGER)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            (1, "offline.invalid", 443, b"authorized", None),
        )


def test_missing_setup_commands_are_ordered_and_scraper_is_deduplicated(
    tmp_path: Path,
) -> None:
    assert setup_hints.missing_setup_commands(tmp_path) == [
        setup_hints.SETUP_COMMAND,
        setup_hints.SCRAPER_COMMAND,
        setup_hints.PROCESSOR_COMMAND,
        setup_hints.BOT_COMMAND,
    ]

    config = tmp_path / "config"
    config.mkdir()
    (config / ".env").write_text("BARABOT_ENV=develop\n")
    (config / "processor.env").touch()
    (config / "bot.token").touch()
    assert setup_hints.missing_setup_commands(tmp_path) == [
        setup_hints.SCRAPER_COMMAND
    ]


def test_next_setup_command_reaches_ready_only_for_complete_setup(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    for name in (".env", "scraper.env", "processor.env", "bot.token"):
        (config / name).touch()

    (config / ".env").write_text("BARABOT_ENV=develop\n")
    assert setup_hints.next_setup_command(tmp_path) == setup_hints.SCRAPER_COMMAND
    _authorized_session(tmp_path)
    assert setup_hints.next_setup_command(tmp_path) == setup_hints.READY_COMMAND
