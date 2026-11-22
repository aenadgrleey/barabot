from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from barabot.core import setup


VALID_API_HASH = "0123456789abcdef0123456789abcdef"


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


def _fail(*args, **kwargs):
    pytest.fail(f"unexpected collaborator call: {args!r} {kwargs!r}")


async def _fail_async(*args, **kwargs):
    _fail(*args, **kwargs)


def test_interactive_setup_configures_missing_components_without_authorizing(
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, Path]] = []

    def configure_scraper(*, path: Path, **_kwargs) -> Path:
        calls.append(("scraper", path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"API_ID=1\nAPI_HASH={VALID_API_HASH}\n")
        return path

    def configure_processor(*, path: Path) -> Path:
        calls.append(("processor", path))
        path.touch()
        return path

    def authorize_bot(_token: str, path: Path) -> SimpleNamespace:
        calls.append(("bot", path))
        path.touch()
        return SimpleNamespace(id=1, username="bot")

    result = asyncio.run(
        setup.setup(
            root=tmp_path,
            input_func=lambda _prompt: "develop",
            secret_input_func=lambda _prompt: "token",
            scraper_credentials=configure_scraper,
            scraper_authorize=_fail_async,
            processor_configure=configure_processor,
            bot_authorize=authorize_bot,
            config_loader=_fail,
        )
    )

    assert result == 0
    assert [name for name, _path in calls] == ["scraper", "processor", "bot"]
    assert not (tmp_path / "config/scraper.session").exists()


def test_headless_setup_applies_complete_groups_and_rejects_partial_group(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def authorize_bot(token: str, path: Path) -> SimpleNamespace:
        path.write_text(token)
        return SimpleNamespace(id=1, username="bot")

    result = asyncio.run(
        setup.setup(
            root=tmp_path,
            headless=True,
            environ={
                "BARABOT_ENV": "prod",
                "API_ID": "7",
                "API_HASH": VALID_API_HASH,
                "OPENAI_API_KEY": "key",
                "BOT_TOKEN": "token",
            },
            input_func=_fail,
            secret_input_func=_fail,
            scraper_authorize=_fail_async,
            bot_authorize=authorize_bot,
            config_loader=_fail,
        )
    )

    assert result == 0
    assert {path.name for path in (tmp_path / "config").iterdir()} == {
        ".env",
        "scraper.env",
        "processor.env",
        "bot.token",
    }

    partial_root = tmp_path / "partial"
    result = asyncio.run(
        setup.setup(
            root=partial_root,
            headless=True,
            environ={"API_ID": "7", "BOT_TOKEN": "secret"},
            bot_authorize=_fail,
            config_loader=_fail,
        )
    )
    assert result != 0
    assert not (partial_root / "config/bot.token").exists()
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "secret" not in output


def test_complete_setup_loads_config(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    for name in (".env", "scraper.env", "processor.env", "bot.token"):
        (config / name).touch()
    _authorized_session(tmp_path)
    loaded: list[bool] = []

    result = asyncio.run(
        setup.setup(
            root=tmp_path,
            input_func=_fail,
            secret_input_func=_fail,
            scraper_credentials=_fail,
            scraper_authorize=_fail_async,
            processor_configure=_fail,
            bot_authorize=_fail,
            config_loader=lambda: loaded.append(True),
        )
    )

    assert result == 0
    assert loaded == [True]


def test_setup_scraper_only_authorizes_session(tmp_path: Path) -> None:
    seen: list[Path] = []

    async def authorize(*, root: Path) -> None:
        seen.append(root)
        _authorized_session(root)

    assert asyncio.run(
        setup.setup_scraper(
            root=tmp_path,
            scraper_authorize=authorize,
            config_loader=_fail,
        )
    ) == 0
    assert seen == [tmp_path]
