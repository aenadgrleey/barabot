from __future__ import annotations

import importlib
import stat
from collections.abc import Callable
from pathlib import Path

import pytest


SECRET = "sk-test-super-secret"
EXPECTED_KEYS = [
    "OPENAI_API_KEY",
    "EMBEDDING_MODEL",
    "EMBEDDING_URL",
    "SIMILARITY_THRESHOLD",
    "EMBEDDING_CONCURRENCY",
    "EMBEDDING_RETRIES",
    "EMBEDDING_TIMEOUT",
]


def _module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    module = importlib.import_module("barabot.domains.processor.configure")
    monkeypatch.setattr(
        module, "PROCESSOR_ENV_PATH", tmp_path / "config" / "processor.env"
    )
    return module


def _answers(*values: str) -> Callable[[str], str]:
    answers = iter(values)
    return lambda _prompt: next(answers)


def _read_env(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert [line.partition("=")[0] for line in lines] == EXPECTED_KEYS
    return dict(line.split("=", 1) for line in lines)


def test_configure_writes_defaults_and_uses_secret_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module(tmp_path, monkeypatch)
    secret_prompts: list[str] = []

    def secret_input(prompt: str) -> str:
        secret_prompts.append(prompt)
        return SECRET

    path = module.configure(
        input_fn=_answers("", "", "", "", "", ""),
        secret_input=secret_input,
    )

    assert path == tmp_path / "config" / "processor.env"
    assert secret_prompts and all(SECRET not in prompt for prompt in secret_prompts)
    assert _read_env(path) == {
        "OPENAI_API_KEY": SECRET,
        "EMBEDDING_MODEL": "text-embedding-3-small",
        "EMBEDDING_URL": "https://api.openai.com/v1/embeddings",
        "SIMILARITY_THRESHOLD": "0.85",
        "EMBEDDING_CONCURRENCY": "4",
        "EMBEDDING_RETRIES": "3",
        "EMBEDDING_TIMEOUT": "30",
    }


def test_configure_writes_custom_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module(tmp_path, monkeypatch)

    path = module.configure(
        input_fn=_answers(
            "custom-model",
            "https://embeddings.example/v1",
            "0.42",
            "8",
            "0",
            "90",
        ),
        secret_input=lambda _prompt: SECRET,
    )

    assert _read_env(path) == {
        "OPENAI_API_KEY": SECRET,
        "EMBEDDING_MODEL": "custom-model",
        "EMBEDDING_URL": "https://embeddings.example/v1",
        "SIMILARITY_THRESHOLD": "0.42",
        "EMBEDDING_CONCURRENCY": "8",
        "EMBEDDING_RETRIES": "0",
        "EMBEDDING_TIMEOUT": "90",
    }


@pytest.mark.parametrize(
    ("secret", "answers", "message"),
    [
        ("   ", ("", "", "", "", "", ""), "OPENAI_API_KEY"),
        (SECRET, ("", "", "-0.01", "", "", ""), "SIMILARITY_THRESHOLD"),
        (SECRET, ("", "", "1.01", "", "", ""), "SIMILARITY_THRESHOLD"),
        (SECRET, ("", "", "not-a-number", "", "", ""), "SIMILARITY_THRESHOLD"),
        (SECRET, ("", "", "", "0", "", ""), "EMBEDDING_CONCURRENCY"),
        (SECRET, ("", "", "", "", "-1", ""), "EMBEDDING_RETRIES"),
        (SECRET, ("", "", "", "", "", "0"), "EMBEDDING_TIMEOUT"),
    ],
)
def test_invalid_values_preserve_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    secret: str,
    answers: tuple[str, ...],
    message: str,
) -> None:
    module = _module(tmp_path, monkeypatch)
    path = module.PROCESSOR_ENV_PATH
    path.parent.mkdir()
    path.write_text("existing configuration\n", encoding="utf-8")

    with pytest.raises(module.ConfigurationError, match=message):
        module.configure(
            input_fn=_answers(*answers), secret_input=lambda _prompt: secret
        )

    assert path.read_text(encoding="utf-8") == "existing configuration\n"


@pytest.mark.parametrize("cancel", [KeyboardInterrupt, EOFError])
def test_cancelled_setup_preserves_existing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancel: type[BaseException],
) -> None:
    module = _module(tmp_path, monkeypatch)
    path = module.PROCESSOR_ENV_PATH
    path.parent.mkdir()
    path.write_text("existing configuration\n", encoding="utf-8")

    def cancelled(_prompt: str) -> str:
        raise cancel()

    with pytest.raises(cancel):
        module.configure(input_fn=cancelled, secret_input=lambda _prompt: SECRET)

    assert path.read_text(encoding="utf-8") == "existing configuration\n"


def test_configure_creates_private_directory_and_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module(tmp_path, monkeypatch)

    path = module.configure(
        input_fn=_answers("", "", "", "", "", ""),
        secret_input=lambda _prompt: SECRET,
    )

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_secret_is_not_leaked_to_output_or_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module = _module(tmp_path, monkeypatch)

    with pytest.raises(module.ConfigurationError) as caught:
        module.configure(
            input_fn=_answers("", "", "invalid", "", "", ""),
            secret_input=lambda _prompt: SECRET,
        )

    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err + str(caught.value)


def test_configure_from_mapping_applies_interactive_defaults_without_prompting(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("barabot.domains.processor.configure")
    path = tmp_path / "config" / "processor.env"

    saved = module.configure_from_mapping(
        {"OPENAI_API_KEY": SECRET},
        path=path,
    )

    assert saved == path
    assert _read_env(path) == {
        "OPENAI_API_KEY": SECRET,
        "EMBEDDING_MODEL": "text-embedding-3-small",
        "EMBEDDING_URL": "https://api.openai.com/v1/embeddings",
        "SIMILARITY_THRESHOLD": "0.85",
        "EMBEDDING_CONCURRENCY": "4",
        "EMBEDDING_RETRIES": "3",
        "EMBEDDING_TIMEOUT": "30",
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_configure_from_mapping_accepts_all_valid_custom_values(tmp_path: Path) -> None:
    module = importlib.import_module("barabot.domains.processor.configure")
    path = tmp_path / "config" / "processor.env"
    values = {
        "OPENAI_API_KEY": SECRET,
        "EMBEDDING_MODEL": "custom-model",
        "EMBEDDING_URL": "https://example.invalid/embeddings",
        "SIMILARITY_THRESHOLD": "0.25",
        "EMBEDDING_CONCURRENCY": "8",
        "EMBEDDING_RETRIES": "0",
        "EMBEDDING_TIMEOUT": "90",
    }

    module.configure_from_mapping(values, path=path)

    assert _read_env(path) == values


@pytest.mark.parametrize(
    ("values", "invalid_name"),
    [
        ({}, "OPENAI_API_KEY"),
        ({"OPENAI_API_KEY": SECRET, "SIMILARITY_THRESHOLD": "nan"}, "SIMILARITY_THRESHOLD"),
        ({"OPENAI_API_KEY": SECRET, "EMBEDDING_CONCURRENCY": "0"}, "EMBEDDING_CONCURRENCY"),
        ({"OPENAI_API_KEY": SECRET, "EMBEDDING_RETRIES": "-1"}, "EMBEDDING_RETRIES"),
        ({"OPENAI_API_KEY": SECRET, "EMBEDDING_TIMEOUT": "0"}, "EMBEDDING_TIMEOUT"),
    ],
)
def test_configure_from_mapping_invalid_source_preserves_existing_file(
    tmp_path: Path,
    values: dict[str, str],
    invalid_name: str,
) -> None:
    module = importlib.import_module("barabot.domains.processor.configure")
    path = tmp_path / "config" / "processor.env"
    path.parent.mkdir()
    original = b"existing processor configuration\n"
    path.write_bytes(original)

    with pytest.raises(module.ConfigurationError, match=invalid_name):
        module.configure_from_mapping(values, path=path)

    assert path.read_bytes() == original


def test_atomic_replace_failure_preserves_processor_file_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = importlib.import_module("barabot.domains.processor.configure")
    path = tmp_path / "config/processor.env"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing processor configuration\n")
    monkeypatch.setattr(
        module.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("simulated replace failure")),
    )

    with pytest.raises(OSError):
        module.configure_from_mapping({"OPENAI_API_KEY": SECRET}, path=path)

    assert path.read_bytes() == b"existing processor configuration\n"
    assert [item.name for item in path.parent.iterdir()] == ["processor.env"]
