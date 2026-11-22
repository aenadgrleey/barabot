from __future__ import annotations

import asyncio
import importlib
import sqlite3
import stat
from pathlib import Path

import pytest


VALID_HASH = "0123456789abcdef0123456789ABCDEF"


class AuthorizedClient:
    def __init__(self, events: list[object]) -> None:
        self.events = events

    async def start(self) -> None:
        self.events.append("start")

    async def is_user_authorized(self) -> bool:
        self.events.append("authorized")
        return True

    async def disconnect(self) -> None:
        self.events.append("disconnect")


def _module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    module = importlib.import_module("barabot.domains.scraper.login")
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    return module


def _write_session(path: Path, auth_key: bytes | None) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE sessions (dc_id INTEGER PRIMARY KEY, server_address TEXT, "
            "port INTEGER, auth_key BLOB, takeout_id INTEGER)"
        )
        connection.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
            (1, "offline.invalid", 443, auth_key, None),
        )


@pytest.mark.parametrize(
    ("session_kind", "expected"),
    [
        ("corrupt", False),
        ("authorized", True),
    ],
)
def test_local_authorization_check_is_offline_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    session_kind: str,
    expected: bool,
) -> None:
    login = _module(monkeypatch, tmp_path)
    path = tmp_path / "scraper.session"
    if session_kind == "corrupt":
        path.write_bytes(b"not sqlite")
    else:
        _write_session(path, b"authorized")

    before = path.read_bytes() if path.exists() else None
    assert login.has_local_authorization(path) is expected
    assert (path.read_bytes() if path.exists() else None) == before
    assert not any((tmp_path / f"scraper.session{s}").exists() for s in ("-wal", "-shm", "-journal"))


def test_credentials_from_mapping_validates_and_normalizes_without_writing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = _module(monkeypatch, tmp_path)
    credentials = login.credentials_from_mapping(
        {"API_ID": " API_ID=12345 ", "API_HASH": f"API_HASH={VALID_HASH}"}
    )
    assert credentials == (12345, VALID_HASH)
    assert list(tmp_path.iterdir()) == []


def test_credentials_from_mapping_reports_only_invalid_variable_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    login = _module(monkeypatch, tmp_path)
    cases = [
        ({"API_ID": "bad", "API_HASH": VALID_HASH}, "API_ID"),
        ({"API_ID": "12345", "API_HASH": "bad"}, "API_HASH"),
    ]
    for mapping, invalid_name in cases:
        with pytest.raises(login.CredentialInputError) as exc_info:
            login.credentials_from_mapping(mapping)
        assert str(exc_info.value) == invalid_name
        assert all(value not in str(exc_info.value) for value in mapping.values())


def test_configure_credentials_from_mapping_atomically_writes_private_file_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = _module(monkeypatch, tmp_path)
    path = tmp_path / "config/scraper.env"

    result = login.configure_credentials_from_mapping(
        {"API_ID": "12345", "API_HASH": VALID_HASH}, path=path
    )

    assert result == path
    assert path.read_text(encoding="utf-8") == f"API_ID=12345\nAPI_HASH={VALID_HASH}\n"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (path.parent / "scraper.session").exists()
    assert [p.name for p in path.parent.iterdir()] == ["scraper.env"]


def test_credentials_atomic_replace_failure_preserves_existing_file_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = _module(monkeypatch, tmp_path)
    path = tmp_path / "config/scraper.env"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old-private-credentials")

    def fail_replace(source, destination) -> None:
        raise OSError("simulated private detail")

    monkeypatch.setattr(login.os, "replace", fail_replace)
    with pytest.raises(OSError):
        login.configure_credentials_from_mapping(
            {"API_ID": "12345", "API_HASH": VALID_HASH}, path=path
        )

    assert path.read_bytes() == b"old-private-credentials"
    assert [p.name for p in path.parent.iterdir()] == ["scraper.env"]
    assert not (path.parent / "scraper.session").exists()


def test_authorize_reuses_persisted_credentials_without_prompting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = _module(monkeypatch, tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "scraper.env").write_text(
        f"API_ID=42\nAPI_HASH={VALID_HASH}\n", encoding="utf-8"
    )
    events: list[object] = []

    def factory(session: str, api_id: int, api_hash: str) -> AuthorizedClient:
        events.append(("factory", session, api_id, api_hash))
        return AuthorizedClient(events)

    asyncio.run(
        login.authorize(
            root=tmp_path,
            input_func=lambda _prompt: pytest.fail("persisted credentials must be reused"),
            secret_input_func=lambda _prompt: pytest.fail("persisted credentials must be reused"),
            client_factory=factory,
        )
    )

    assert events == [
        ("factory", str(config / "scraper.session"), 42, VALID_HASH),
        "start",
        "authorized",
        "disconnect",
    ]


def test_authorize_collects_missing_credentials_before_telegram_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = _module(monkeypatch, tmp_path)
    events: list[object] = []

    def factory(session: str, api_id: int, api_hash: str) -> AuthorizedClient:
        events.append(("factory", session, api_id, api_hash))
        return AuthorizedClient(events)

    asyncio.run(
        login.authorize(
            root=tmp_path,
            input_func=lambda _prompt: "12345",
            secret_input_func=lambda _prompt: VALID_HASH,
            client_factory=factory,
        )
    )

    assert (tmp_path / "config/scraper.env").exists()
    assert events[0] == (
        "factory",
        str(tmp_path / "config/scraper.session"),
        12345,
        VALID_HASH,
    )


def test_authorize_rejects_unauthorized_client_and_still_disconnects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = _module(monkeypatch, tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "scraper.env").write_text(f"API_ID=1\nAPI_HASH={VALID_HASH}\n")
    events: list[str] = []

    class Client:
        async def start(self) -> None:
            events.append("start")

        async def is_user_authorized(self) -> bool:
            events.append("authorized")
            return False

        async def disconnect(self) -> None:
            events.append("disconnect")

    with pytest.raises(RuntimeError, match="authoriz"):
        asyncio.run(login.authorize(root=tmp_path, client_factory=lambda *_args: Client()))
    assert events == ["start", "authorized", "disconnect"]


def test_authorize_makes_created_session_and_parent_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = _module(monkeypatch, tmp_path)
    config = tmp_path / "config"
    config.mkdir(mode=0o755)
    (config / "scraper.env").write_text(f"API_ID=1\nAPI_HASH={VALID_HASH}\n")

    class Client:
        async def start(self) -> None:
            (config / "scraper.session").write_bytes(b"authorized session")
            (config / "scraper.session").chmod(0o644)

        async def is_user_authorized(self) -> bool:
            return True

        async def disconnect(self) -> None:
            return None

    asyncio.run(login.authorize(root=tmp_path, client_factory=lambda *_args: Client()))

    assert stat.S_IMODE(config.stat().st_mode) == 0o700
    assert stat.S_IMODE((config / "scraper.session").stat().st_mode) == 0o600
