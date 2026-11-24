from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest

from barabot.domains.bot import auth

TOKEN = "123456:super-secret-token"


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class TestAuthorize:
    def test_persists_only_raw_token_with_private_permissions(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        token_path = tmp_path / "config" / "bot.token"

        identity = auth.authorize(
            TOKEN,
            token_path,
            verifier=lambda _token: auth.BotIdentity(id=42, username="safe_bot"),
        )

        assert identity == auth.BotIdentity(id=42, username="safe_bot")
        assert token_path.read_bytes() == TOKEN.encode()
        assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        captured = capsys.readouterr()
        assert TOKEN not in captured.out + captured.err

    def test_replace_failure_preserves_token_and_cleans_temp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token_path = tmp_path / "config/bot.token"
        token_path.parent.mkdir(parents=True)
        token_path.write_bytes(b"existing-token")
        monkeypatch.setattr(
            auth.os,
            "replace",
            lambda *_args: (_ for _ in ()).throw(OSError("simulated replace failure")),
        )

        with pytest.raises(OSError):
            auth.authorize(
                TOKEN,
                token_path,
                verifier=lambda _token: auth.BotIdentity(id=42, username="safe_bot"),
            )

        assert token_path.read_bytes() == b"existing-token"
        assert [item.name for item in token_path.parent.iterdir()] == ["bot.token"]

    def test_failed_verification_preserves_existing_token_without_secret_leak(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        token_path = tmp_path / "config" / "bot.token"
        token_path.parent.mkdir()
        token_path.write_text("existing-token")

        def fail(_token: str):
            raise RuntimeError(f"request failed for {_token}")

        with pytest.raises(auth.AuthorizationError) as caught:
            auth.authorize(TOKEN, token_path, verifier=fail)

        assert token_path.read_text() == "existing-token"
        assert TOKEN not in str(caught.value)
        captured = capsys.readouterr()
        assert TOKEN not in captured.out + captured.err


class TestVerifyToken:
    def test_parses_safe_bot_identity_without_network(self) -> None:
        requested: list[tuple[object, float]] = []

        def opener(request: object, timeout: float):
            requested.append((request, timeout))
            return FakeResponse({"ok": True, "result": {"id": 42, "username": "safe_bot"}})

        identity = auth.verify_token(TOKEN, opener=opener)

        assert identity == auth.BotIdentity(id=42, username="safe_bot")
        assert len(requested) == 1

    def test_rejects_malformed_response_without_leaking(self) -> None:
        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        with pytest.raises(auth.AuthorizationError) as caught:
            auth.verify_token(TOKEN, opener=lambda *_args, **_kwargs: Response(b"not-json"))

        assert TOKEN not in str(caught.value)

    def test_sanitizes_secret_bearing_transport_errors(self) -> None:
        def fail(*_args: object, **_kwargs: object):
            raise OSError(f"failed URL containing {TOKEN}")

        with pytest.raises(auth.AuthorizationError) as caught:
            auth.verify_token(TOKEN, opener=fail)

        assert TOKEN not in str(caught.value)
