import sqlite3
from pathlib import Path

import pytest

from barabot.core import setup_hints


SETUP_COMMAND = ".venv/bin/python -m barabot setup"
SCRAPER_COMMAND = ".venv/bin/python -m barabot setup scraper"
PROCESSOR_COMMAND = ".venv/bin/python -m barabot processor configure"
BOT_COMMAND = ".venv/bin/python -m barabot bot auth <token>"
READY_COMMAND = ".venv/bin/python -m barabot run --check"


ALL_SETUP_PATHS = (
    Path("config/.env"),
    Path("config/scraper.env"),
    Path("config/scraper.session"),
    Path("config/processor.env"),
    Path("config/bot.token"),
)


def _create(root: Path, *paths: Path) -> None:
    for relative_path in paths:
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative_path == Path("config/scraper.session"):
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "CREATE TABLE sessions ("
                    "dc_id INTEGER PRIMARY KEY, server_address TEXT, port INTEGER, "
                    "auth_key BLOB, takeout_id INTEGER)"
                )
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                    (1, "offline.invalid", 443, b"authorized", None),
                )
        else:
            path.touch()


@pytest.mark.parametrize(
    ("missing", "expected"),
    [
        (
            {Path("config/.env")},
            [SETUP_COMMAND],
        ),
        (
            {Path("config/scraper.env")},
            [SCRAPER_COMMAND],
        ),
        (
            {Path("config/scraper.session")},
            [SCRAPER_COMMAND],
        ),
        (
            {Path("config/processor.env")},
            [PROCESSOR_COMMAND],
        ),
        (
            {Path("config/bot.token")},
            [BOT_COMMAND],
        ),
    ],
)
def test_returns_command_for_each_missing_setup_file(
    tmp_path: Path, missing: set[Path], expected: list[str]
) -> None:
    _create(tmp_path, *(set(ALL_SETUP_PATHS) - missing))

    assert setup_hints.missing_setup_commands(tmp_path) == expected


def test_returns_only_relevant_commands_for_multiple_missing_files(
    tmp_path: Path,
) -> None:
    _create(tmp_path, Path("config/scraper.env"), Path("config/scraper.session"))

    assert setup_hints.missing_setup_commands(tmp_path) == [
        SETUP_COMMAND,
        PROCESSOR_COMMAND,
        BOT_COMMAND,
    ]


def test_deduplicates_setup_scraper_when_both_scraper_files_are_missing(
    tmp_path: Path,
) -> None:
    _create(
        tmp_path,
        Path("config/.env"),
        Path("config/processor.env"),
        Path("config/bot.token"),
    )

    assert setup_hints.missing_setup_commands(tmp_path) == [SCRAPER_COMMAND]


def test_returns_no_commands_when_setup_files_exist(tmp_path: Path) -> None:
    _create(tmp_path, *ALL_SETUP_PATHS)

    assert setup_hints.missing_setup_commands(tmp_path) == []


@pytest.mark.parametrize(
    "session_state",
    ["zero-byte", "corrupt", "wrong-schema", "null-auth-key", "empty-auth-key"],
)
def test_existing_unauthorized_session_still_recommends_setup_scraper(
    tmp_path: Path, session_state: str
) -> None:
    session_relative = Path("config/scraper.session")
    _create(tmp_path, *(path for path in ALL_SETUP_PATHS if path != session_relative))
    session_path = tmp_path / session_relative
    if session_state == "zero-byte":
        session_path.touch()
    elif session_state == "corrupt":
        session_path.write_bytes(b"not a SQLite database")
    elif session_state == "wrong-schema":
        with sqlite3.connect(session_path) as connection:
            connection.execute("CREATE TABLE unrelated (value BLOB)")
    else:
        with sqlite3.connect(session_path) as connection:
            connection.execute(
                "CREATE TABLE sessions ("
                "dc_id INTEGER PRIMARY KEY, server_address TEXT, port INTEGER, "
                "auth_key BLOB, takeout_id INTEGER)"
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
                (
                    1,
                    "offline.invalid",
                    443,
                    None if session_state == "null-auth-key" else b"",
                    None,
                ),
            )

    assert setup_hints.missing_setup_commands(tmp_path) == [SCRAPER_COMMAND]
    assert setup_hints.next_setup_command(tmp_path) == SCRAPER_COMMAND


def test_next_setup_command_uses_component_order_then_ready_fallback(
    tmp_path: Path,
) -> None:
    assert setup_hints.next_setup_command(tmp_path) == SETUP_COMMAND

    _create(tmp_path, Path("config/.env"))
    assert setup_hints.next_setup_command(tmp_path) == SCRAPER_COMMAND

    _create(tmp_path, Path("config/scraper.env"), Path("config/scraper.session"))
    assert setup_hints.next_setup_command(tmp_path) == PROCESSOR_COMMAND

    _create(tmp_path, Path("config/processor.env"))
    assert setup_hints.next_setup_command(tmp_path) == BOT_COMMAND

    _create(tmp_path, Path("config/bot.token"))
    assert setup_hints.next_setup_command(tmp_path) == READY_COMMAND


def test_run_check_guidance_puts_setup_first_and_deduplicates_details(
    tmp_path: Path,
) -> None:
    assert setup_hints.setup_guidance_commands(tmp_path) == [
        SETUP_COMMAND,
        SCRAPER_COMMAND,
        PROCESSOR_COMMAND,
        BOT_COMMAND,
    ]