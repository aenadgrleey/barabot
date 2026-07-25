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
    "SESSION_STRING",
    "OPENAI_API_KEY",
    "EMBEDDING_MODEL",
    "EMBEDDING_URL",
    "SIMILARITY_THRESHOLD",
    "WATCH_CHATS",
    "LOG_LEVEL",
    "EMBEDDING_CONCURRENCY",
    "EMBEDDING_RETRIES",
    "EMBEDDING_TIMEOUT",
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
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")


def test_deploy_environment_has_exactly_develop_and_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    config, _ = _import_config(monkeypatch)

    assert [(item.name, item.value) for item in config.DeployEnvironment] == [
        ("develop", "develop"),
        ("prod", "prod"),
    ]


def test_config_env_is_loaded_only_by_load_from_fixed_config_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, dotenv_calls = _import_config(monkeypatch)
    _set_valid_environment(monkeypatch, "develop")

    assert dotenv_calls == []
    config.load()

    project_root = Path(__file__).resolve().parents[2]
    assert dotenv_calls == [
        ((project_root / "config" / "scraper.env",), {"override": False}),
        ((project_root / "config" / "processor.env",), {"override": False}),
        ((project_root / "config" / ".env",), {"override": False})
    ]


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
            "OPENAI_API_KEY": "processor-key",
            "EMBEDDING_MODEL": "domain-model",
        },
        project_root / "config" / ".env": {
            "BARABOT_ENV": "develop",
            "API_ID": "99999",
            "OPENAI_API_KEY": "legacy-key",
            "EMBEDDING_MODEL": "legacy-model",
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
    assert loaded.openai_api_key == "processor-key"
    assert loaded.embedding_model == "domain-model"
    assert loaded.log_level == "WARNING"


@pytest.mark.parametrize("raw", ["develop", "prod"])
def test_load_parses_deploy_environment(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    config, _ = _import_config(monkeypatch)
    _set_valid_environment(monkeypatch, raw)

    loaded = config.load()

    assert loaded.environment is config.DeployEnvironment(raw)


def test_config_uses_fixed_repository_config_and_data_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = _import_config(monkeypatch)
    _set_valid_environment(monkeypatch, "develop")

    loaded = config.load()

    project_root = Path(__file__).resolve().parents[2]
    assert config.PROJECT_ROOT == project_root
    assert config.Config.bot_token_path == project_root / "config" / "bot.token"
    assert loaded.scraper_session_path == project_root / "config" / "scraper.session"
    assert loaded.db_path == project_root / "data" / "barabot.sqlite"
    assert loaded.lock_path == project_root / "data" / "barabot.lock"
    assert loaded.pid_path == project_root / "data" / "barabot.pid"


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


def test_session_string_is_not_required_or_exposed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, _ = _import_config(monkeypatch)
    _set_valid_environment(monkeypatch, "develop")
    monkeypatch.delenv("SESSION_STRING", raising=False)

    loaded = config.load()

    assert not hasattr(loaded, "session_string")
    assert loaded.scraper_session_path.name == "scraper.session"
