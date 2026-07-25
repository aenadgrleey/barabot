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
        ("missing", False),
        ("zero-byte", False),
        ("corrupt", False),
        ("wrong-schema", False),
        ("null-auth-key", False),
        ("empty-auth-key", False),
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
    if session_kind == "zero-byte":
        path.touch()
    elif session_kind == "corrupt":
        path.write_bytes(b"not sqlite")
    elif session_kind == "wrong-schema":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE unrelated (value BLOB)")
    elif session_kind in {"null-auth-key", "empty-auth-key", "authorized"}:
        auth_key = {
            "null-auth-key": None,
            "empty-auth-key": b"",
            "authorized": b"authorized",
        }[session_kind]
        _write_session(path, auth_key)

    before = path.read_bytes() if path.exists() else None
    assert login.has_local_authorization(path) is expected
    assert (path.read_bytes() if path.exists() else None) == before
    assert not any((tmp_path / f"scraper.session{s}").exists() for s in ("-wal", "-shm", "-journal"))


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        ("12345", 12345),
        (" API_ID = 12345 ", 12345),
        ("API_ID = '12345'", 12345),
        ('API_ID = "12345"', 12345),
    ],
)
def test_api_id_accepts_raw_and_matching_assignment_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, submitted: str, expected: int
) -> None:
    assert _module(monkeypatch, tmp_path)._valid_api_id(submitted) == expected


@pytest.mark.parametrize(
    "submitted",
    ["0", "-1", "abc", "API_HASH=12345", "OTHER=12345", "API_ID='12345\"", "API_ID='12345' trailing"],
)
def test_api_id_rejects_wrong_keys_malformed_quotes_and_invalid_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, submitted: str
) -> None:
    assert _module(monkeypatch, tmp_path)._valid_api_id(submitted) is None


@pytest.mark.parametrize(
    "submitted",
    [VALID_HASH, f" API_HASH = {VALID_HASH} ", f"API_HASH='{VALID_HASH}'", f'API_HASH="{VALID_HASH}"'],
)
def test_api_hash_accepts_raw_and_matching_assignment_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, submitted: str
) -> None:
    assert _module(monkeypatch, tmp_path)._valid_api_hash(submitted) == VALID_HASH


@pytest.mark.parametrize(
    "submitted",
    ["short", "g" * 32, f"API_ID={VALID_HASH}", f"OTHER={VALID_HASH}", f"API_HASH='{VALID_HASH}' trailing"],
)
def test_api_hash_rejects_wrong_keys_and_invalid_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, submitted: str
) -> None:
    assert _module(monkeypatch, tmp_path)._valid_api_hash(submitted) is None


def test_credentials_from_mapping_validates_and_normalizes_without_writing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    login = _module(monkeypatch, tmp_path)
    credentials = login.credentials_from_mapping(
        {"API_ID": " API_ID=12345 ", "API_HASH": f"API_HASH={VALID_HASH}"}
    )
    assert credentials == (12345, VALID_HASH)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("mapping", "invalid_name"),
    [
        ({"API_ID": "bad", "API_HASH": VALID_HASH}, "API_ID"),
        ({"API_ID": "12345", "API_HASH": "bad"}, "API_HASH"),
    ],
)
def test_credentials_from_mapping_reports_only_invalid_variable_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mapping: dict[str, str],
    invalid_name: str,
) -> None:
    login = _module(monkeypatch, tmp_path)
    with pytest.raises(login.CredentialInputError) as exc_info:
        login.credentials_from_mapping(mapping)
    assert str(exc_info.value) == invalid_name
    for value in mapping.values():
        assert value not in str(exc_info.value)


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


def test_interactive_configure_credentials_retries_and_never_creates_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    login = _module(monkeypatch, tmp_path)
    path = tmp_path / "config/scraper.env"
    ids = iter(["bad-id", "12345"])
    hashes = iter(["bad-hash", VALID_HASH])

    assert login.configure_credentials(
        path=path,
        input_func=lambda _prompt: next(ids),
        secret_input_func=lambda _prompt: next(hashes),
    ) == path

    assert path.exists()
    assert not (path.parent / "scraper.session").exists()
    output = capsys.readouterr().out
    assert "bad-id" not in output
    assert "bad-hash" not in output
    assert VALID_HASH not in output


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


@pytest.mark.parametrize("cancel_type", [KeyboardInterrupt, EOFError])
@pytest.mark.parametrize(
    "boundary",
    ["api-id-prompt", "api-hash-prompt", "client-factory", "client-start", "authorization"],
)
def test_authorize_propagates_cancellation_without_leaking_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    boundary: str,
    cancel_type: type[BaseException],
) -> None:
    login = _module(monkeypatch, tmp_path)
    cancel = cancel_type("sentinel-secret")
    events: list[str] = []

    def input_id(_prompt: str) -> str:
        if boundary == "api-id-prompt":
            raise cancel
        return "12345"

    def input_hash(_prompt: str) -> str:
        if boundary == "api-hash-prompt":
            raise cancel
        return VALID_HASH

    class Client:
        async def start(self) -> None:
            events.append("start")
            if boundary == "client-start":
                raise cancel

        async def is_user_authorized(self) -> bool:
            events.append("authorized")
            if boundary == "authorization":
                raise cancel
            return True

        async def disconnect(self) -> None:
            events.append("disconnect")

    def factory(*_args):
        if boundary == "client-factory":
            raise cancel
        return Client()

    with pytest.raises(cancel_type):
        asyncio.run(
            login.authorize(
                root=tmp_path,
                input_func=input_id,
                secret_input_func=input_hash,
                client_factory=factory,
            )
        )

    if boundary in {"client-start", "authorization"}:
        assert events[-1] == "disconnect"
    captured = capsys.readouterr()
    assert "sentinel-secret" not in captured.out + captured.err
