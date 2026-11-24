from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import dotenv
import pytest


_ENV_VARS = (
    "BARABOT_ENV",
    "API_ID",
    "API_HASH",
    "WATCH_CHATS",
    "LOG_LEVEL",
    "TYPESAFE_API_KEY",
)


def _import_config(monkeypatch: pytest.MonkeyPatch):
    dotenv_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_load_dotenv(*args: object, **kwargs: object) -> bool:
        dotenv_calls.append((args, kwargs))
        return False

    monkeypatch.setattr(dotenv, "load_dotenv", fake_load_dotenv)
    sys.modules.pop("barabot.core.config", None)
    module = importlib.import_module("barabot.core.config")
    return module, dotenv_calls


def _set_valid_environment(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BARABOT_ENV", environment)
    monkeypatch.setenv("API_ID", "12345")
    monkeypatch.setenv("API_HASH", "a" * 32)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-typesafe-key")


def test_load_merges_domain_files_before_process_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = _import_config(monkeypatch)
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    project_root = Path(__file__).resolve().parents[2]
    file_values = {
        project_root / "config" / "scraper.env": {
            "API_ID": "12345",
            "API_HASH": "a" * 32,
        },
        project_root / "config" / "processor.env": {
            "TYPESAFE_API_KEY": "processor-key",
        },
        project_root / "config" / ".env": {
            "BARABOT_ENV": "develop",
            "API_ID": "99999",
            "TYPESAFE_API_KEY": "legacy-key",
            "LOG_LEVEL": "DEBUG",
        },
    }

    def fake_load(path: Path, *, override: bool) -> bool:
        assert override is False
        for name, value in file_values[path].items():
            if name not in os.environ:
                monkeypatch.setenv(name, value)
        return True

    monkeypatch.setattr(config, "load_dotenv", fake_load)

    loaded = config.load()

    assert loaded.api_id == 12345
    assert loaded.typesafe_api_key == "processor-key"
    assert loaded.log_level == "WARNING"


@pytest.mark.parametrize("raw", ["develop", "prod"])
def test_load_parses_deploy_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    config, _ = _import_config(monkeypatch)
    _set_valid_environment(monkeypatch, raw)

    loaded = config.load()

    assert loaded.environment is config.DeployEnvironment(raw)


def test_missing_barabot_env_fails_with_accepted_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = _import_config(monkeypatch)
    _set_valid_environment(monkeypatch, "develop")
    monkeypatch.delenv("BARABOT_ENV")

    with pytest.raises(RuntimeError, match=r"BARABOT_ENV.*develop.*prod"):
        config.load()


def test_invalid_barabot_env_fails_with_value_and_accepted_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = _import_config(monkeypatch)
    _set_valid_environment(monkeypatch, "staging")

    with pytest.raises(RuntimeError, match=r"BARABOT_ENV.*staging.*develop.*prod"):
        config.load()


def test_telegram_backend_file_and_override(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from barabot.core.telegram import TelegramBackend, TelegramConfigError

    monkeypatch.delenv("TELEGRAM_BACKEND")
    monkeypatch.setitem(sys.modules, "tgmux", SimpleNamespace(SharedClient=object))
    path = tmp_path / "config" / ".env"
    path.parent.mkdir()
    assert TelegramBackend.configured(tmp_path).resolve().client_type is object
    path.write_text("TELEGRAM_BACKEND=telethon\n")
    assert TelegramBackend.configured(tmp_path).resolve().uses_local_session
    monkeypatch.setenv("TELEGRAM_BACKEND", "tgmux")
    assert TelegramBackend.configured(tmp_path).resolve().client_type is object
    monkeypatch.setenv("TELEGRAM_BACKEND", "typo")
    with pytest.raises(TelegramConfigError, match="TELEGRAM_BACKEND"):
        TelegramBackend.configured(tmp_path)
