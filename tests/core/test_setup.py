from __future__ import annotations

import asyncio
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRAPER_COMMAND = ".venv/bin/python -m barabot setup scraper"
READY_COMMAND = ".venv/bin/python -m barabot run --check"
VALID_API_HASH = "0123456789abcdef0123456789abcdef"


def _authorized_session(root: Path, auth_key: bytes | None = b"authorized") -> Path:
    path = root / "config/scraper.session"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, server_address TEXT, "
            "port INTEGER, auth_key BLOB, takeout_id INTEGER)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            (1, "offline.invalid", 443, auth_key, None),
        )
    return path


def _touch(root: Path, *names: str) -> None:
    config = root / "config"
    config.mkdir(parents=True, exist_ok=True)
    for name in names:
        (config / name).touch()


def _unexpected(*args, **kwargs):
    pytest.fail(f"unexpected collaborator call: {args!r} {kwargs!r}")


async def _unexpected_async(*args, **kwargs):
    pytest.fail(f"unexpected async collaborator call: {args!r} {kwargs!r}")


def _run(setup_module, **kwargs) -> int:
    return asyncio.run(setup_module.setup(**kwargs))


def test_plain_setup_configures_credentials_and_other_components_but_defers_authorization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from barabot.core import setup as setup_module

    calls: list[object] = []

    def credentials(*, path: Path, input_func, secret_input_func) -> Path:
        calls.append(("credentials", path))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"API_ID=1\nAPI_HASH={VALID_API_HASH}\n", encoding="utf-8")
        return path

    def processor(*, path: Path) -> Path:
        calls.append(("processor", path))
        path.touch()
        return path

    def bot(token: str, path: Path):
        calls.append(("bot", path))
        path.write_text(token, encoding="utf-8")
        return SimpleNamespace(id=1, username="bot")

    result = _run(
        setup_module,
        root=tmp_path,
        input_func=lambda _prompt: "develop",
        secret_input_func=lambda _prompt: "safe-test-token",
        scraper_credentials=credentials,
        scraper_authorize=_unexpected_async,
        processor_configure=processor,
        bot_authorize=bot,
        config_loader=_unexpected,
    )

    assert result == 0
    assert calls == [
        ("credentials", tmp_path / "config/scraper.env"),
        ("processor", tmp_path / "config/processor.env"),
        ("bot", tmp_path / "config/bot.token"),
    ]
    assert not (tmp_path / "config/scraper.session").exists()
    output = capsys.readouterr().out
    assert "pending" in output.lower()
    assert SCRAPER_COMMAND in output
    assert "Setup complete" not in output


def test_plain_setup_with_existing_credentials_never_enters_telegram_authorization(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from barabot.core import setup as setup_module

    _touch(tmp_path, ".env", "scraper.env", "processor.env", "bot.token")
    loads: list[str] = []
    result = _run(
        setup_module,
        root=tmp_path,
        input_func=_unexpected,
        secret_input_func=_unexpected,
        scraper_credentials=_unexpected,
        scraper_authorize=_unexpected_async,
        processor_configure=_unexpected,
        bot_authorize=_unexpected,
        config_loader=lambda: loads.append("load"),
    )

    assert result == 0
    assert loads == []
    assert SCRAPER_COMMAND in capsys.readouterr().out


def test_complete_setup_is_the_only_state_that_loads_config_and_claims_complete(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from barabot.core import setup as setup_module

    _touch(tmp_path, ".env", "scraper.env", "processor.env", "bot.token")
    _authorized_session(tmp_path)
    loads: list[str] = []

    assert _run(
        setup_module,
        root=tmp_path,
        input_func=_unexpected,
        secret_input_func=_unexpected,
        scraper_credentials=_unexpected,
        scraper_authorize=_unexpected_async,
        processor_configure=_unexpected,
        bot_authorize=_unexpected,
        config_loader=lambda: loads.append("load") or SimpleNamespace(),
    ) == 0

    assert loads == ["load"]
    output = capsys.readouterr().out
    assert "Setup complete" in output
    assert READY_COMMAND in output


def test_setup_scraper_wrapper_authorizes_then_advances_to_next_missing_component(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from barabot.core import setup as setup_module

    _touch(tmp_path, ".env", "scraper.env")
    calls: list[Path] = []

    async def authorize(*, root: Path) -> None:
        calls.append(root)
        _authorized_session(root)

    result = asyncio.run(
        setup_module.setup_scraper(root=tmp_path, scraper_authorize=authorize)
    )

    assert result == 0
    assert calls == [tmp_path]
    assert capsys.readouterr().out.strip().endswith(
        ".venv/bin/python -m barabot processor configure"
    )


def test_setup_scraper_reuses_credentials_and_does_not_load_runtime_config(
    tmp_path: Path
) -> None:
    from barabot.core import setup as setup_module

    _touch(tmp_path, "scraper.env")
    seen: list[Path] = []

    async def authorize(*, root: Path) -> None:
        assert (root / "config/scraper.env").exists()
        seen.append(root)
        _authorized_session(root)

    assert asyncio.run(
        setup_module.setup_scraper(
            root=tmp_path,
            scraper_authorize=authorize,
            config_loader=_unexpected,
        )
    ) == 0
    assert seen == [tmp_path]


@pytest.mark.parametrize(
    ("environ", "created"),
    [
        ({}, set()),
        ({"BARABOT_ENV": "prod"}, {".env"}),
        ({"API_ID": "7", "API_HASH": VALID_API_HASH}, {"scraper.env"}),
        ({"OPENAI_API_KEY": "safe-key"}, {"processor.env"}),
        ({"BOT_TOKEN": "123:safe"}, {"bot.token"}),
    ],
)
def test_headless_applies_each_supplied_group_independently_without_prompting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
    created: set[str],
) -> None:
    from barabot.core import setup as setup_module

    def bot(token: str, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        return SimpleNamespace(id=1, username="bot")

    assert _run(
        setup_module,
        root=tmp_path,
        headless=True,
        environ=environ,
        input_func=_unexpected,
        secret_input_func=_unexpected,
        scraper_authorize=_unexpected_async,
        bot_authorize=bot,
        config_loader=_unexpected,
    ) == 0

    config = tmp_path / "config"
    actual = {p.name for p in config.iterdir()} if config.exists() else set()
    assert actual == created
    assert not (config / "scraper.session").exists()
    output = capsys.readouterr().out
    assert "Setup complete" not in output
    assert SCRAPER_COMMAND in output


def test_headless_applies_later_complete_groups_when_earlier_groups_are_absent(
    tmp_path: Path
) -> None:
    from barabot.core import setup as setup_module

    def bot(token: str, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        return SimpleNamespace(id=1, username="bot")

    assert _run(
        setup_module,
        root=tmp_path,
        headless=True,
        environ={"OPENAI_API_KEY": "safe-key", "BOT_TOKEN": "123:safe"},
        input_func=_unexpected,
        secret_input_func=_unexpected,
        scraper_authorize=_unexpected_async,
        bot_authorize=bot,
        config_loader=_unexpected,
    ) == 0
    assert (tmp_path / "config/processor.env").exists()
    assert (tmp_path / "config/bot.token").exists()
    assert not (tmp_path / "config/.env").exists()
    assert not (tmp_path / "config/scraper.env").exists()


@pytest.mark.parametrize(
    ("environ", "missing_name", "later_name"),
    [
        ({"API_ID": "7", "BOT_TOKEN": "123:safe"}, "API_HASH", "bot.token"),
        ({"EMBEDDING_MODEL": "model", "BOT_TOKEN": "123:safe"}, "OPENAI_API_KEY", "bot.token"),
    ],
)
def test_headless_partial_group_fails_before_later_groups_and_names_only_missing_variable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
    missing_name: str,
    later_name: str,
) -> None:
    from barabot.core import setup as setup_module

    result = _run(
        setup_module,
        root=tmp_path,
        headless=True,
        environ=environ,
        input_func=_unexpected,
        secret_input_func=_unexpected,
        scraper_authorize=_unexpected_async,
        bot_authorize=_unexpected,
        config_loader=_unexpected,
    )

    assert result != 0
    assert not (tmp_path / "config" / later_name).exists()
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert missing_name in combined
    for value in environ.values():
        assert value not in combined


def test_headless_ignores_session_b64_and_never_calls_importer_or_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from barabot.core import setup as setup_module
    from barabot.domains.scraper import login

    assert not hasattr(login, "install_authorized_session")
    assert _run(
        setup_module,
        root=tmp_path,
        headless=True,
        environ={
            "API_ID": "7",
            "API_HASH": VALID_API_HASH,
            "BARABOT_SCRAPER_SESSION_B64": "must-be-ignored",
        },
        input_func=_unexpected,
        secret_input_func=_unexpected,
        scraper_authorize=_unexpected_async,
        config_loader=_unexpected,
    ) == 0
    assert (tmp_path / "config/scraper.env").exists()
    assert not (tmp_path / "config/scraper.session").exists()


def test_headless_force_replaces_only_supplied_groups_and_preserves_session_bytes(
    tmp_path: Path
) -> None:
    from barabot.core import setup as setup_module

    _touch(tmp_path, ".env", "scraper.env", "processor.env", "bot.token")
    session = _authorized_session(tmp_path)
    before = {p.name: p.read_bytes() for p in (tmp_path / "config").iterdir()}
    loads: list[str] = []

    assert _run(
        setup_module,
        root=tmp_path,
        headless=True,
        force=True,
        environ={"BARABOT_ENV": "prod"},
        input_func=_unexpected,
        secret_input_func=_unexpected,
        scraper_authorize=_unexpected_async,
        bot_authorize=_unexpected,
        config_loader=lambda: loads.append("load") or SimpleNamespace(),
    ) == 0

    assert (tmp_path / "config/.env").read_text() == "BARABOT_ENV=prod\n"
    for name in ("scraper.env", "processor.env", "bot.token"):
        assert (tmp_path / "config" / name).read_bytes() == before[name]
    assert session.read_bytes() == before["scraper.session"]
    assert loads == ["load"]


def test_headless_partial_failure_preserves_earlier_completed_component(
    tmp_path: Path
) -> None:
    from barabot.core import setup as setup_module

    assert _run(
        setup_module,
        root=tmp_path,
        headless=True,
        environ={"BARABOT_ENV": "prod", "API_ID": "7"},
        input_func=_unexpected,
        secret_input_func=_unexpected,
        scraper_authorize=_unexpected_async,
        config_loader=_unexpected,
    ) != 0
    assert (tmp_path / "config/.env").read_text() == "BARABOT_ENV=prod\n"
    assert not (tmp_path / "config/scraper.env").exists()


def test_environment_replace_failure_preserves_existing_private_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from barabot.core import setup as setup_module

    path = tmp_path / "config/.env"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"BARABOT_ENV=develop\n")
    monkeypatch.setattr(
        setup_module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("simulated replace failure")),
    )

    assert _run(
        setup_module,
        root=tmp_path,
        headless=True,
        force=True,
        environ={"BARABOT_ENV": "prod"},
        config_loader=_unexpected,
    ) != 0
    assert path.read_bytes() == b"BARABOT_ENV=develop\n"
    assert [item.name for item in path.parent.iterdir()] == [".env"]


def test_headless_environment_creates_private_directory_and_file(tmp_path: Path) -> None:
    from barabot.core import setup as setup_module

    assert _run(
        setup_module,
        root=tmp_path,
        headless=True,
        environ={"BARABOT_ENV": "prod"},
        config_loader=_unexpected,
    ) == 0
    path = tmp_path / "config/.env"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
